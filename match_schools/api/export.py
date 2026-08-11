# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Export any table the frontend shows to Excel (xlsx) or PDF.

The frontend sends the columns it is currently displaying — including the
user's own show/hide and ordering choices — so the export always matches what
is on screen.
"""

import io
from datetime import date, datetime

import frappe
from frappe import _
from frappe.utils import flt, today
from frappe.utils.pdf import get_pdf

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

# Only these datasets may be exported, and each maps to the endpoint that
# already enforces the caller's scope — so an export can never leak more than
# the matching screen would show.
DATASETS = {
	"students": ("match_schools.api.students", "list_students", "قائمة الطلاب"),
	"fees": ("match_schools.api.fees", "list_fees", "الرسوم المالية"),
	"grades": ("match_schools.api.academics", "list_grades", "الدرجات"),
	"exams": ("match_schools.api.academics", "list_exams", "جدول الامتحانات"),
	"classes": ("match_schools.api.academics", "list_classes", "الصفوف والشُعب"),
	"subjects": ("match_schools.api.academics", "list_subjects", "المواد الدراسية"),
	"teachers": ("match_schools.api.academics", "list_teachers", "المعلمون"),
	"assignments": ("match_schools.api.assignments", "list_assignments", "الواجبات"),
	"behaviour": ("match_schools.api.wellbeing", "list_behaviour", "السلوك والانضباط"),
	"books": ("match_schools.api.resources", "list_books", "المكتبة"),
	"loans": ("match_schools.api.resources", "list_loans", "إعارات الكتب"),
	"transport": ("match_schools.api.resources", "list_transport_assignments", "النقل المدرسي"),
	"guardians": ("match_schools.api.students", "list_guardians", "أولياء الأمور"),
	"health": ("match_schools.api.wellbeing", "list_health_records", "السجل الصحي"),
	"routes": ("match_schools.api.resources", "list_routes", "خطوط النقل"),
}


def _fetch_rows(dataset: str, params: dict) -> list[dict]:
	"""Call the dataset's own list endpoint so permissions and scoping apply."""
	if dataset not in DATASETS:
		frappe.throw(_("Unknown dataset: {0}").format(dataset))

	module_path, fn_name, _label = DATASETS[dataset]
	module = frappe.get_module(module_path)
	fn = getattr(module, fn_name)

	# Ask for everything, not just the current page.
	params = dict(params or {})
	params.setdefault("page", 1)
	params.setdefault("page_size", 2000)

	result = fn(**params)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result

	if isinstance(data, dict) and "items" in data:
		return data["items"]
	if isinstance(data, list):
		return data
	return []


def _stringify(value) -> str:
	if value is None:
		return ""
	if isinstance(value, bool):
		return "نعم" if value else "لا"
	if isinstance(value, (datetime, date)):
		return str(value)
	if isinstance(value, (list, tuple)):
		return ", ".join(str(v) for v in value)
	if isinstance(value, dict):
		return frappe.as_json(value)
	return str(value)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def export_excel(
	dataset: str,
	columns: str | list = None,
	filters: str | dict = None,
	title: str = None,
	persona: str = None,
):
	"""Build an .xlsx of the given dataset and return it as a download.

	`columns` is [{"fieldname": ..., "label": ...}, ...] in display order.
	"""
	columns = parse_json_arg(columns) or []
	filters = parse_json_arg(filters) or {}

	if not columns:
		return fail(
			message_en="No columns selected for export.",
			message_ar="لم يتم اختيار أي أعمدة للتصدير.",
		)

	rows = _fetch_rows(dataset, filters)
	label = title or DATASETS.get(dataset, (None, None, dataset))[2]

	try:
		from openpyxl import Workbook
		from openpyxl.styles import Alignment, Font, PatternFill
		from openpyxl.utils import get_column_letter
	except ImportError:
		return fail(
			message_en="openpyxl is not installed on the server.",
			message_ar="مكتبة التصدير غير مثبتة على الخادم.",
		)

	wb = Workbook()
	ws = wb.active
	ws.title = label[:31] or "Export"
	ws.sheet_view.rightToLeft = True  # Arabic reading order

	header_font = Font(bold=True, color="FFFFFF")
	header_fill = PatternFill("solid", fgColor="0550AE")
	centre = Alignment(horizontal="center", vertical="center")

	for col_idx, col in enumerate(columns, start=1):
		cell = ws.cell(row=1, column=col_idx, value=col.get("label") or col.get("fieldname"))
		cell.font = header_font
		cell.fill = header_fill
		cell.alignment = centre

	for row_idx, row in enumerate(rows, start=2):
		for col_idx, col in enumerate(columns, start=1):
			raw = row.get(col.get("fieldname"))
			# Keep numbers numeric so Excel can total them.
			if isinstance(raw, (int, float)) and not isinstance(raw, bool):
				ws.cell(row=row_idx, column=col_idx, value=raw)
			else:
				ws.cell(row=row_idx, column=col_idx, value=_stringify(raw))

	# Size columns to their content, within reason.
	for col_idx, col in enumerate(columns, start=1):
		longest = len(str(col.get("label") or ""))
		for row in rows[:200]:
			longest = max(longest, len(_stringify(row.get(col.get("fieldname")))))
		ws.column_dimensions[get_column_letter(col_idx)].width = min(max(longest + 2, 10), 48)

	ws.freeze_panes = "A2"
	ws.auto_filter.ref = (
		f"A1:{get_column_letter(len(columns))}{len(rows) + 1}" if rows else None
	)

	buffer = io.BytesIO()
	wb.save(buffer)

	filename = f"{label}-{today()}.xlsx"
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = buffer.getvalue()
	frappe.local.response.type = "binary"
	frappe.local.response[
		"content_type"
	] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def export_pdf(
	dataset: str,
	columns: str | list = None,
	filters: str | dict = None,
	title: str = None,
	orientation: str = "Landscape",
	persona: str = None,
):
	"""Render the dataset as an A4 PDF using a school-branded RTL template."""
	columns = parse_json_arg(columns) or []
	filters = parse_json_arg(filters) or {}

	if not columns:
		return fail(
			message_en="No columns selected for export.",
			message_ar="لم يتم اختيار أي أعمدة للتصدير.",
		)

	rows = _fetch_rows(dataset, filters)
	label = title or DATASETS.get(dataset, (None, None, dataset))[2]

	html = _render_table_html(label, columns, rows, filters)

	pdf = get_pdf(html, {"orientation": orientation or "Landscape"})

	frappe.local.response.filename = f"{label}-{today()}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _render_table_html(label: str, columns: list, rows: list, filters: dict) -> str:
	"""A print-ready RTL document: school header, filter strip, table, footer."""
	school = _school_header()

	head_cells = "".join(
		f"<th>{frappe.utils.escape_html(str(c.get('label') or c.get('fieldname')))}</th>"
		for c in columns
	)

	body_rows = []
	for idx, row in enumerate(rows, start=1):
		cells = [f'<td class="idx">{idx}</td>']
		for c in columns:
			value = _stringify(row.get(c.get("fieldname")))
			css = "num" if isinstance(row.get(c.get("fieldname")), (int, float)) else "txt"
			cells.append(f'<td class="{css}">{frappe.utils.escape_html(value)}</td>')
		body_rows.append("<tr>" + "".join(cells) + "</tr>")

	filter_chips = "".join(
		f'<span class="chip"><b>{frappe.utils.escape_html(str(k))}:</b> '
		f'{frappe.utils.escape_html(_stringify(v))}</span>'
		for k, v in (filters or {}).items()
		if v not in (None, "", [], {}) and k not in ("page", "page_size")
	)

	return f"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 10mm 8mm; }}
  * {{ box-sizing: border-box; font-family: "Cairo", "Noto Naskh Arabic", sans-serif; }}
  body {{ direction: rtl; text-align: right; color: #0d1117; font-size: 9pt; }}
  .head {{ display: flex; justify-content: space-between; align-items: flex-end;
           border-bottom: 2.5px solid #0d1117; padding-bottom: 8px; margin-bottom: 10px; }}
  .title {{ font-size: 15pt; font-weight: 700; color: #0550ae; }}
  .sub {{ font-size: 8pt; color: #4a5568; margin-top: 2px; }}
  .school {{ text-align: left; }}
  .school-name {{ font-size: 13pt; font-weight: 700; }}
  .chips {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 5px;
            padding: 6px 10px; margin-bottom: 10px; font-size: 8pt; }}
  .chip {{ margin-left: 14px; }}
  table {{ width: 100%; border-collapse: collapse; border: 1px solid #d0d7de; }}
  thead tr {{ background: #0550ae; color: #fff; }}
  th {{ padding: 5px 6px; font-size: 7.5pt; font-weight: 700; text-align: center;
        border-left: 1px solid rgba(255,255,255,.25); }}
  th:last-child {{ border-left: none; }}
  tbody tr:nth-child(even) {{ background: #f6f8fa; }}
  td {{ padding: 4px 6px; border-bottom: 1px solid #d0d7de;
        border-left: 1px solid #eaecef; text-align: center; }}
  td:last-child {{ border-left: none; }}
  td.txt {{ text-align: right; }}
  td.num {{ text-align: left; direction: ltr; font-variant-numeric: tabular-nums; }}
  td.idx {{ color: #4a5568; font-size: 7.5pt; }}
  tfoot td {{ font-weight: 700; background: #eef2f7; }}
  .foot {{ margin-top: 12px; border-top: 1px solid #d0d7de; padding-top: 5px;
           display: flex; justify-content: space-between; font-size: 7pt; color: #4a5568; }}
  .brand {{ font-weight: 700; color: #0550ae; }}
  thead {{ display: table-header-group; }}
  tr {{ page-break-inside: avoid; }}
</style>
</head>
<body>
  <div class="head">
    <div>
      <div class="title">{frappe.utils.escape_html(label)}</div>
      <div class="sub">عدد السجلات: {len(rows)}{_year_suffix()}</div>
    </div>
    <div class="school">
      <div class="school-name">{frappe.utils.escape_html(school)}</div>
      <div class="sub">تاريخ الطباعة: {frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M")}</div>
    </div>
  </div>

  {f'<div class="chips">{filter_chips}</div>' if filter_chips else ''}

  <table>
    <thead><tr><th style="width:4%">#</th>{head_cells}</tr></thead>
    <tbody>{''.join(body_rows) if body_rows else f'<tr><td colspan="{len(columns) + 1}">لا توجد بيانات</td></tr>'}</tbody>
  </table>

  <div class="foot">
    <span>{frappe.utils.escape_html(school)} — مستند داخلي</span>
    <span class="brand">Match Education</span>
  </div>
</body>
</html>
"""


def _school_header() -> str:
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not company:
		return "Match Education"
	return frappe.db.get_value("Company", company, "company_name") or company


def _year_suffix() -> str:
	year = get_default_academic_year()
	return f" — العام الدراسي {year}" if year else ""


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def export_report_card(student: str, academic_term: str = None, persona: str = None):
	"""A printable report card for one student."""
	from match_schools.api.gradebook import term_grades

	result = term_grades(student=student, academic_term=academic_term, persona=persona)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result

	# Fall back to the older Assessment Result table for schools that still
	# grade through exams only, without the gradebook.
	if not data or not data.get("subjects"):
		from match_schools.api.academics import report_card

		legacy = report_card(student=student, academic_term=academic_term)
		data = legacy.get("data") if isinstance(legacy, dict) and "success" in legacy else legacy

	if not data or not data.get("subjects"):
		return fail(
			message_en="No results recorded for this student.",
			message_ar="لا توجد نتائج مسجّلة لهذا الطالب.",
		)

	# A teacher has no school-wide average, and an unpublished term withholds
	# one. Printing flt(None) would put a false 0% on the card.
	if data.get("shows_overall") is False:
		return fail(
			message_en="The term average is not available to you yet.",
			message_ar="المعدل الفصلي غير متاح — بانتظار اعتماد الإدارة ونشر النتائج.",
		)

	html = _render_report_card_html(data)
	pdf = get_pdf(html, {"orientation": "Portrait"})

	frappe.local.response.filename = f"report-card-{student}-{today()}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _render_report_card_html(data: dict) -> str:
	"""The term report card, on the school's shared letterhead."""
	from match_schools.api.document_theme import document_shell, verdict_pill

	def _compare(mine: float, average) -> str:
		"""An average with an arrow showing which side of it the mark sits.

		wkhtmltopdf's WebKit has no emoji font, so the arrows are drawn with
		geometric characters that every Arabic font carries — an emoji here
		would print as an empty box on the certificate a family keeps.
		"""
		if average is None:
			return "—"
		average = flt(average)
		diff = mine - average
		if diff > 0.05:
			mark, colour = "▲", "#22543d"
		elif diff < -0.05:
			mark, colour = "▼", "#742a2a"
		else:
			mark, colour = "=", "#718096"
		return (
			f'<span class="num">{average:g}%</span> '
			f'<span style="color:{colour};font-weight:700">{mark}</span>'
		)

	def _row(s: dict) -> str:
		# Gradebook subjects carry `final`/`course`; legacy ones `percentage`/`subject`.
		name = s.get("course") or s.get("subject") or ""
		pct = flt(s.get("final") if s.get("final") is not None else s.get("percentage"))
		return f"""<tr>
			<td class="txt">{frappe.utils.escape_html(str(name))}</td>
			<td class="num">{pct:g} / 100</td>
			<td>{frappe.utils.escape_html(str(s.get('grade') or '—'))}</td>
			<td>{_compare(pct, s.get('section_average'))}</td>
			<td>{_compare(pct, s.get('grade_average'))}</td>
			<td>{verdict_pill(pct)}</td>
		</tr>"""

	rows = "".join(_row(s) for s in data["subjects"])
	average = flt(data.get("overall") if data.get("overall") is not None else data.get("average"))
	verdict = "ناجح" if average >= 50 else "راسب"

	body = f"""
  <table class="data">
    <thead>
      <tr><th>المادة</th><th>العلامة النهائية</th><th>التقدير</th>
          <th>معدل الشعبة</th><th>معدل الصف</th><th>النتيجة</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <div class="panel">
    <div class="panel-head">النتيجة النهائية</div>
    <table class="panel-body">
      <tr>
        <td><div class="panel-k">المعدل العام</div>
            <div class="panel-v num">{average:g}%</div></td>
        <td><div class="panel-k">التقدير</div>
            <div class="panel-v">{frappe.utils.escape_html(str((data.get('overall_grade') or {}).get('grade') or '—'))}</div></td>
        <td><div class="panel-k">النتيجة</div>
            <div class="panel-v">{verdict}</div></td>
        <td><div class="panel-k">عدد المواد</div>
            <div class="panel-v num">{len(data['subjects'])}</div></td>
      </tr>
    </table>
  </div>
"""

	return document_shell(
		title="بطاقة الدرجات",
		subtitle="نتيجة الفصل الدراسي",
		facts=[
			("الطالب", data.get("student_name") or ""),
			("الرقم", data.get("student") or ""),
			("العام الدراسي", data.get("academic_year") or ""),
			("الفصل", data.get("academic_term") or ""),
		],
		body=body,
		reference=f"REF: {data.get('student') or ''}",
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def export_quarter_report_card(
	student: str = None,
	student_group: str = None,
	quarter: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""A report card for one quarter — the "شهادة الشهرين" a school issues mid-term.

	Built from the same calculation the gradebook shows, so a mark on this card
	and a mark on the screen can never disagree. Each subject is reported out of
	that quarter's own marks (40, say) rather than out of 100, because that is
	the figure a family is given.
	"""
	if not student or not quarter:
		return fail(
			message_en="A student and a quarter are required.",
			message_ar="يجب تحديد الطالب والربع.",
		)

	from match_schools.api.assessment_plan import compute_marks

	groups = [student_group] if student_group else [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group", "active": 1},
			fields=["parent"],
			limit=10,
		)
	]
	if not groups:
		return fail(
			message_en="This student is not in any class.",
			message_ar="الطالب غير مسجّل في أي شعبة.",
		)

	courses = sorted(
		{
			r.course
			for r in frappe.get_all(
				"MS Gradebook Entry",
				filters={
					"student": student,
					**({"academic_term": academic_term} if academic_term else {}),
				},
				fields=["course"],
				limit_page_length=0,
			)
			if r.course
		}
	)
	if not courses:
		return fail(
			message_en="No marks recorded for this student.",
			message_ar="لا توجد علامات مرصودة لهذا الطالب.",
		)

	subjects = []
	quarter_total = 0.0
	earned_total = 0.0

	for course in courses:
		for group in groups:
			res = compute_marks(
				student_group=group,
				course=course,
				academic_term=academic_term,
				student=student,
				persona=persona,
			)
			payload = res.get("data") if isinstance(res, dict) and "data" in res else res
			if not payload or not payload.get("students"):
				continue
			row = payload["students"][0]
			q = next((x for x in row["quarters"] if x["quarter"] == quarter), None)
			if not q:
				continue
			# A subject with nothing assessed in this quarter is left off the
			# certificate rather than printed as 0/40. Counting it as a zero
			# would tell a family the child failed a subject that was simply
			# not examined yet — and would drag the average down with it.
			if not any(c.get("counted") or c.get("dropped") for c in q.get("categories") or []):
				continue
			subjects.append(
				{
					"course": course,
					"marks": flt(q["marks"]),
					"totalMarks": flt(q["totalMarks"]),
					"percent": flt(q["percent"]),
				}
			)
			quarter_total += flt(q["totalMarks"])
			earned_total += flt(q["marks"])
			break

	if not subjects:
		return fail(
			message_en=f"No marks have been recorded in {quarter} for this student yet.",
			message_ar=f"لم تُرصد أي علامات في {quarter} لهذا الطالب حتى الآن.",
		)

	profile = frappe.db.get_value(
		"Student", student, ["student_name", "name"], as_dict=True
	) or {}
	enrolment = frappe.db.get_value(
		"Program Enrollment",
		{"student": student, "docstatus": ["<", 2]},
		["program", "student_batch_name", "academic_year"],
		as_dict=True,
	) or {}

	average = (earned_total / quarter_total * 100) if quarter_total else 0.0

	html = _render_quarter_card_html(
		{
			"student": profile.get("name") or student,
			"student_name": profile.get("student_name") or student,
			"program": enrolment.get("program"),
			"batch": enrolment.get("student_batch_name"),
			"academic_year": enrolment.get("academic_year"),
			"academic_term": academic_term,
			"quarter": quarter,
			"subjects": subjects,
			"earned": round(earned_total, 2),
			"total": round(quarter_total, 2),
			"average": round(average, 2),
		}
	)
	pdf = get_pdf(html, {"orientation": "Portrait"})

	frappe.local.response.filename = f"quarter-{quarter}-{student}-{today()}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _render_quarter_card_html(data: dict) -> str:
	"""The two-month certificate, on the school's shared letterhead."""
	from match_schools.api.document_theme import document_shell, verdict_pill

	rows = "".join(
		f"""<tr>
			<td class="txt">{frappe.utils.escape_html(str(s['course']))}</td>
			<td class="num">{flt(s['marks']):g} / {flt(s['totalMarks']):g}</td>
			<td class="num">{flt(s['percent']):g}%</td>
			<td>{verdict_pill(flt(s['percent']))}</td>
		</tr>"""
		for s in data["subjects"]
	)

	average = flt(data.get("average"))
	verdict = "ناجح" if average >= 50 else "راسب"

	body = f"""
  <table class="data">
    <thead>
      <tr><th>المادة</th><th>العلامة</th><th>النسبة</th><th>التقدير</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <div class="panel">
    <div class="panel-head">ملخص {frappe.utils.escape_html(str(data.get('quarter') or ''))}</div>
    <table class="panel-body">
      <tr>
        <td><div class="panel-k">المجموع</div>
            <div class="panel-v num">{flt(data.get('earned')):g} / {flt(data.get('total')):g}</div></td>
        <td><div class="panel-k">المعدل</div>
            <div class="panel-v num">{average:g}%</div></td>
        <td><div class="panel-k">النتيجة</div>
            <div class="panel-v">{verdict}</div></td>
        <td><div class="panel-k">عدد المواد</div>
            <div class="panel-v num">{len(data['subjects'])}</div></td>
      </tr>
    </table>
  </div>
"""

	return document_shell(
		title=f"شهادة {data.get('quarter') or ''}",
		subtitle="تقرير نتائج فترة دراسية",
		facts=[
			("الطالب", data.get("student_name") or ""),
			("الرقم", data.get("student") or ""),
			("الصف", f"{data.get('program') or ''} {data.get('batch') or ''}".strip()),
			("العام الدراسي", data.get("academic_year") or ""),
		],
		body=body,
		note=(
			f"هذه الشهادة تعرض نتائج {data.get('quarter') or ''} فقط، "
			"ولا تمثّل النتيجة النهائية للفصل الدراسي."
		),
		reference=f"REF: {data.get('student') or ''}",
	)


def _verdict_badge(percent: float) -> str:
	if percent >= 90:
		return "ممتاز"
	if percent >= 80:
		return "جيد جداً"
	if percent >= 65:
		return "جيد"
	if percent >= 50:
		return "مقبول"
	return "راسب"


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def quarter_results(
	student_group: str = None,
	quarter: str = None,
	academic_term: str = None,
	course: str = None,
	persona: str = None,
):
	"""A class's quarter results on screen, before anyone prints anything.

	The filter the school actually wants: pick a class and a quarter, see every
	student's mark out of that quarter's total, and print the cards from there.
	Passing a course narrows it to one subject.
	"""
	if not student_group or not quarter:
		return fail(
			message_en="A class and a quarter are required.",
			message_ar="يجب تحديد الشعبة والربع.",
		)

	from match_schools.api.assessment_plan import compute_marks

	courses = [course] if course else sorted(
		{
			r.course
			for r in frappe.get_all(
				"MS Gradebook Entry",
				filters={
					"student_group": student_group,
					**({"academic_term": academic_term} if academic_term else {}),
				},
				fields=["course"],
				limit_page_length=0,
			)
			if r.course
		}
	)
	if not courses:
		return {"students": [], "courses": [], "quarter": quarter}

	# student -> {course: marks}, built once so the table is a single pass.
	by_student: dict[str, dict] = {}
	quarter_total = 0.0

	for c in courses:
		res = compute_marks(
			student_group=student_group,
			course=c,
			academic_term=academic_term,
			persona=persona,
		)
		payload = res.get("data") if isinstance(res, dict) and "data" in res else res
		if not payload or not payload.get("students"):
			continue
		for row in payload["students"]:
			q = next((x for x in row["quarters"] if x["quarter"] == quarter), None)
			if not q:
				continue
			quarter_total = flt(q["totalMarks"])
			entry = by_student.setdefault(
				row["student"],
				{"student": row["student"], "studentName": row["studentName"], "subjects": {}},
			)
			entry["subjects"][c] = {
				"marks": flt(q["marks"]),
				"totalMarks": flt(q["totalMarks"]),
				"percent": flt(q["percent"]),
			}

	students = []
	for entry in by_student.values():
		marks = sum(s["marks"] for s in entry["subjects"].values())
		total = sum(s["totalMarks"] for s in entry["subjects"].values())
		students.append(
			{
				**entry,
				"total": round(marks, 2),
				"outOf": round(total, 2),
				"average": round(marks / total * 100, 2) if total else 0.0,
			}
		)
	students.sort(key=lambda s: -s["average"])

	return {
		"quarter": quarter,
		"quarterTotal": quarter_total,
		"courses": courses,
		"students": students,
		"classAverage": (
			round(sum(s["average"] for s in students) / len(students), 2) if students else 0.0
		),
	}
