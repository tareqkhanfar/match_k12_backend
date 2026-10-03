# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""«النماذج» in a student's file — every document written about this pupil.

Forms, evaluations and requests are each filed by the screen that made them:
a counselling form under its category, a monthly report under its section,
a transfer under the transfers page. Looking for "everything about this
child" meant visiting all of them. This module gathers them into one list
and says, for each, which existing endpoint prints it — so the file shows a
document exactly as its own screen would print it, and nothing is rendered
twice.
"""

import json

import frappe
from frappe.utils import cint, escape_html, flt, get_fullname

from match_schools.api.utils import BACK_OFFICE, fail, ms_endpoint

KINDS = {
	"form": "نموذج",
	"periodic": "تقييم دوري",
	"evaluation": "تقييم مهارات وسلوك",
	"request": "طلب",
}
TRANSFER_STATUS = {"Draft": "مسودة", "Completed": "منفَّذ", "Cancelled": "ملغى"}
ADMISSION_STATUS = {"Draft": "مسودة", "Confirmed": "مؤكَّد", "Transferred": "رُحّل", "Cancelled": "ملغى"}
FORM_STATUS = {"Draft": "مسودة", "Submitted": "مُعتمد", "Completed": "مكتمل"}


def _who(user: str | None) -> str:
	return get_fullname(user) if user else ""


def _forms(student: str) -> list[dict]:
	from match_schools.api.forms import CATEGORIES as labels
	return [
		{
			"kind": "form",
			"id": e.name,
			"title": e.template_title or e.template,
			"subtitle": labels.get(e.category, e.category or ""),
			"date": str(e.filled_on or e.creation or "")[:16],
			"by": _who(e.filled_by),
			"status": FORM_STATUS.get(e.status, e.status or ""),
			"print": {"method": "forms.print_entry", "params": {"entry": e.name}},
		}
		for e in frappe.get_all(
			"MS Form Entry",
			filters={"student": student},
			fields=["name", "template", "template_title", "category", "status", "filled_by", "filled_on", "creation"],
			order_by="creation desc",
			limit_page_length=300,
		)
	]


def _periodic(student: str) -> list[dict]:
	if not frappe.db.table_exists("MS Periodic Entry"):
		return []
	rows = frappe.db.sql(
		"""select form, period, student_group, count(*) as n, max(evaluated_on) as last_on
		   from `tabMS Periodic Entry` where student = %s
		   group by form, period, student_group order by last_on desc""",
		student,
		as_dict=True,
	)
	titles = {
		r.name: r.title
		for r in frappe.get_all("MS Periodic Form", filters={"name": ["in", [r.form for r in rows] or [""]]}, fields=["name", "title"])
	}
	groups = {
		r.name: r.student_group_name or r.name
		for r in frappe.get_all(
			"Student Group",
			filters={"name": ["in", [r.student_group for r in rows if r.student_group] or [""]]},
			fields=["name", "student_group_name"],
		)
	}
	return [
		{
			"kind": "periodic",
			"id": f"{r.form}|{r.period}|{r.student_group}",
			"title": f"{titles.get(r.form, r.form)} — {r.period}",
			"subtitle": f"{groups.get(r.student_group, r.student_group or '')} · {cint(r.n)} تقييماً",
			"date": str(r.last_on or "")[:16],
			"by": "",
			"status": "",
			"print": {
				"method": "periodic_evaluations.print_cards",
				"params": {
					"form": r.form,
					"period": r.period,
					"student_group": r.student_group,
					"students": json.dumps([student]),
				},
			},
		}
		for r in rows
		if r.student_group
	]


def _evaluations(student: str) -> list[dict]:
	return [
		{
			"kind": "evaluation",
			"id": e.name,
			"title": e.form_title or e.form,
			"subtitle": " · ".join(
				x for x in [e.course or "", f"{flt(e.percent):g}%" if flt(e.max_score) else ""] if x
			),
			"date": str(e.evaluated_on or "")[:16],
			"by": _who(e.evaluated_by),
			"status": "منشور" if cint(e.is_published) else "",
			"print": {"method": "student_forms.print_evaluation", "params": {"entry": e.name}},
		}
		for e in frappe.get_all(
			"MS Evaluation Entry",
			filters={"student": student},
			fields=["name", "form", "form_title", "course", "percent", "max_score", "evaluated_by", "evaluated_on", "is_published"],
			order_by="evaluated_on desc",
			limit_page_length=300,
		)
	]


def _requests(student: str) -> list[dict]:
	out = []
	if frappe.db.table_exists("MS Student Transfer"):
		for t in frappe.get_all(
			"MS Student Transfer",
			filters={"student": student},
			fields=["name", "status", "transfer_date", "to_school", "creation", "owner"],
			order_by="creation desc",
		):
			out.append(
				{
					"kind": "request",
					"id": t.name,
					"title": "طلب نقل وشهادة انتقال" + (f" — إلى {t.to_school}" if t.to_school else ""),
					"subtitle": f"تاريخ النقل {t.transfer_date}" if t.transfer_date else "",
					"date": str(t.creation or "")[:16],
					"by": _who(t.owner),
					"status": TRANSFER_STATUS.get(t.status, t.status or ""),
					"print": {"method": "transfers.preview", "params": {"transfer": t.name}},
				}
			)
	# The request a pupil was admitted from is linked to their application,
	# and the application to the student record.
	applicant = frappe.db.get_value("Student", student, "student_applicant")
	if applicant and frappe.db.table_exists("MS Admission Request"):
		for r in frappe.get_all(
			"MS Admission Request",
			filters={"application": applicant},
			fields=["name", "status", "program", "academic_year", "creation", "owner"],
			order_by="creation desc",
		):
			out.append(
				{
					"kind": "request",
					"id": r.name,
					"title": "طلب متسع (خطاب القبول)",
					"subtitle": " · ".join(x for x in [r.program or "", r.academic_year or ""] if x),
					"date": str(r.creation or "")[:16],
					"by": _who(r.owner),
					"status": ADMISSION_STATUS.get(r.status, r.status or ""),
					"print": {"method": "admission_requests.preview", "params": {"request": r.name}},
				}
			)
	return out


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def student_documents(student: str = None, persona: str = None):
	"""Every form, evaluation and request on record for this student, newest
	first, each with the endpoint that prints it."""
	if not student or not frappe.db.exists("Student", student):
		return fail(message_en="Student not found.", message_ar="لم يتم العثور على الطالب.")
	items = _forms(student) + _periodic(student) + _evaluations(student) + _requests(student)
	items.sort(key=lambda i: i["date"] or "", reverse=True)
	counts = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
	return {"items": items, "counts": counts, "kinds": KINDS}


def _letterhead() -> str:
	"""The school letterhead image its letters use, or its name when none."""
	from match_schools.api.document_theme import school_name
	from match_schools.api.forms_print import file_data_uri

	rows = frappe.get_all(
		"MS Form Template",
		filters={"print_logo": ["is", "set"], "print_theme": "letter"},
		pluck="print_logo",
		order_by="modified desc",
		limit=1,
	)
	if rows:
		return f'<div class="ms-banner"><img src="{file_data_uri(rows[0])}" alt=""></div>'
	return f'<div class="ev-school">{escape_html(school_name())}</div>'


EVAL_CSS = """<style>
  .ev-school { text-align: center; font-weight: 800; font-size: 18px; margin-bottom: 4mm; }
  .ev-title { text-align: center; font-weight: 800; font-size: 19px; margin: 2mm 0 5mm; }
  .ev-t { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  .ev-t td, .ev-t th { border: 1px solid #1f2433; padding: 5px 8px; }
  .ev-t th { background: #eef0f5; font-weight: 700; }
  .ev-info td { width: 33%; }
  .ev-cat td { background: #f6f7fa; font-weight: 700; }
  .ev-gap { height: 4mm; }
  .ev-foot { margin-top: 8mm; display: table; width: 100%; font-size: 14px; }
  .ev-foot > div { display: table-cell; }
</style>"""


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def print_evaluation(entry: str = None, persona: str = None):
	"""A skills or behaviour assessment as a printable page — those forms
	have no print of their own."""
	from match_schools.api.forms_print import PRINT_STYLE

	if not entry or not frappe.db.exists("MS Evaluation Entry", entry):
		return fail(message_en="Not found.", message_ar="لم يتم العثور على التقييم.")
	doc = frappe.get_doc("MS Evaluation Entry", entry)
	group = (
		frappe.db.get_value("Student Group", doc.student_group, "student_group_name") or doc.student_group
		if doc.student_group
		else ""
	)
	rows, category = [], None
	for a in doc.answers or []:
		if (a.category or "") != (category or "") and a.category:
			rows.append(f'<tr class="ev-cat"><td colspan="3">{escape_html(a.category)}</td></tr>')
		category = a.category
		rows.append(
			f"<tr><td>{escape_html(a.item or '')}</td>"
			f"<td style='text-align:center'>{escape_html(a.value_label or (f'{flt(a.score):g}' if a.score else ''))}</td>"
			f"<td>{escape_html(a.note or '')}</td></tr>"
		)
	total = (
		f"<p>المجموع: <b>{flt(doc.total_score):g} / {flt(doc.max_score):g}</b> ({flt(doc.percent):g}%)</p>"
		if flt(doc.max_score)
		else ""
	)
	body = (
		_letterhead()
		+ f'<div class="ev-title">{escape_html(doc.form_title or doc.form)}</div>'
		+ '<table class="ev-t ev-info"><tr>'
		+ f"<td>الطالب: <b>{escape_html(doc.student_name or '')}</b></td>"
		+ f"<td>الشعبة: <b>{escape_html(group)}</b></td>"
		+ f"<td>المادة: <b>{escape_html(doc.course or '—')}</b></td>"
		+ '</tr></table><div class="ev-gap"></div>'
		+ '<table class="ev-t"><thead><tr><th>البند</th><th style="width:22%">التقييم</th><th style="width:30%">ملاحظة</th></tr></thead>'
		+ f"<tbody>{''.join(rows) or '<tr><td colspan=3>—</td></tr>'}</tbody></table>"
		+ total
		+ (f"<p>ملاحظات: {escape_html(doc.notes)}</p>" if doc.notes else "")
		+ '<div class="ev-foot">'
		+ f"<div>المقيِّم: {escape_html(_who(doc.evaluated_by))}</div>"
		+ f"<div style='text-align:left'>التاريخ: {escape_html(str(doc.evaluated_on or '')[:10])}</div>"
		+ "</div>"
	)
	return {
		"html": PRINT_STYLE + EVAL_CSS + f'<div class="ms-page theme-soft">{body}</div>',
		"title": f"{doc.form_title} — {doc.student_name}",
	}
