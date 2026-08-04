# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Official school documents.

The report card covers one term. These are the documents a parent actually
walks into the office to ask for:

  * شهادة قيد — proof the child is enrolled this year
  * كشف علامات — the full multi-year transcript with a cumulative average
  * شهادة تخرج — awarded on completing the final year
  * شهادة حسن سيرة وسلوك — conduct, drawn from the behaviour record

Each is rendered server-side as an A4 PDF and carries a verification code, so
a document presented elsewhere can be checked against the school's records.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import flt, format_date, get_datetime, getdate, now_datetime, today
from frappe.utils.pdf import get_pdf

from match_k12.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_year,
	k12_endpoint,
	resolve_scope,
)

DOCUMENTS = {
	"enrolment": "شهادة قيد",
	"transcript": "كشف علامات",
	"graduation": "شهادة تخرج",
	"conduct": "شهادة حسن سيرة وسلوك",
}


def _assert_can_read(persona: str, student: str):
	"""Students and parents may only pull their own documents."""
	if persona in BACK_OFFICE:
		return
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		frappe.throw(
			_("Official documents are issued by the administration."), frappe.PermissionError
		)
	if student not in (scope.get("students") or []):
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)


def _verification_code(kind: str, student: str) -> str:
	"""A short, stable code the office can check a presented document against."""
	raw = f"{kind}:{student}:{today()}"
	return hashlib.sha256(raw.encode()).hexdigest()[:10].upper()


def _school() -> dict:
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not company:
		return {"name": "", "phone": "", "email": ""}
	row = (
		frappe.db.get_value(
			"Company", company, ["company_name", "phone_no", "email"], as_dict=True
		)
		or {}
	)
	return {
		"name": row.get("company_name") or company,
		"phone": row.get("phone_no") or "",
		"email": row.get("email") or "",
	}


def _student_brief(student: str) -> dict:
	row = (
		frappe.db.get_value(
			"Student",
			student,
			[
				"student_name", "date_of_birth", "gender", "nationality",
				"joining_date", "image", "student_email_id",
			],
			as_dict=True,
		)
		or {}
	)
	enrollment = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": ["<", 2]},
		fields=["program", "academic_year", "student_batch_name"],
		order_by="creation desc",
		limit=1,
	)
	return {
		"id": student,
		"name": row.get("student_name") or student,
		"date_of_birth": str(row.get("date_of_birth") or ""),
		"gender": row.get("gender") or "",
		"nationality": row.get("nationality") or "",
		"joined": str(row.get("joining_date") or ""),
		"program": enrollment[0].program if enrollment else None,
		"batch": enrollment[0].student_batch_name if enrollment else None,
		"academic_year": enrollment[0].academic_year if enrollment else None,
	}


# --- Rendering -------------------------------------------------------------

PAGE_CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: "Cairo", "Tahoma", sans-serif;
  direction: rtl; text-align: right;
  color: #1e1b3a; font-size: 12px; margin: 0;
}
.frame { border: 2px solid #4f46e5; border-radius: 10px; padding: 22px 26px; }
.head { text-align: center; border-bottom: 2px solid #4f46e5; padding-bottom: 14px; }
.school { font-size: 20px; font-weight: 800; color: #4f46e5; }
.contact { font-size: 10px; color: #6b7280; margin-top: 4px; }
.doc-title {
  margin: 22px auto 6px; text-align: center; font-size: 17px; font-weight: 800;
  border: 1.5px solid #4f46e5; border-radius: 999px; padding: 7px 26px; display: inline-block;
}
.center { text-align: center; }
.body-text { line-height: 2.1; font-size: 13px; margin-top: 18px; }
.body-text strong { color: #4f46e5; }
table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 11px; }
th, td { border: 1px solid #d9d7ea; padding: 7px 9px; text-align: right; }
th { background: #eef2ff; font-weight: 700; color: #3730a3; }
tr:nth-child(even) td { background: #fafafe; }
.num { direction: ltr; unicode-bidi: isolate; text-align: center; }
.facts { width: 100%; margin-top: 16px; }
.facts td { border: none; padding: 5px 0; }
.facts .k { color: #6b7280; width: 130px; }
.facts .v { font-weight: 700; }
.term-head { background: #4f46e5; color: #fff; font-weight: 700; padding: 6px 9px; margin-top: 16px; border-radius: 5px; }
.total { background: #eef2ff; font-weight: 800; }
.sign { margin-top: 42px; display: flex; justify-content: space-between; }
.sign div { text-align: center; width: 45%; font-size: 11px; }
.sign .line { border-top: 1px solid #9ca3af; margin-top: 42px; padding-top: 5px; color: #6b7280; }
.foot { margin-top: 26px; border-top: 1px dashed #d9d7ea; padding-top: 9px;
        font-size: 9px; color: #9ca3af; display: flex; justify-content: space-between; }
.code { font-family: monospace; letter-spacing: 1px; color: #4f46e5; font-weight: 700; }
"""


def _wrap(title: str, inner: str, code: str) -> str:
	school = _school()
	contact = " • ".join(x for x in (school["phone"], school["email"]) if x)
	issued = format_date(today(), "dd-MM-yyyy")
	return f"""<!doctype html><html><head><meta charset="utf-8">
<style>{PAGE_CSS}</style></head><body>
<div class="frame">
  <div class="head">
    <div class="school">{frappe.utils.escape_html(school['name'])}</div>
    <div class="contact">{frappe.utils.escape_html(contact)}</div>
  </div>
  <div class="center"><div class="doc-title">{title}</div></div>
  {inner}
  <div class="sign">
    <div><div class="line">توقيع مدير المدرسة</div></div>
    <div><div class="line">الختم الرسمي</div></div>
  </div>
  <div class="foot">
    <span>تاريخ الإصدار: <span class="num">{issued}</span></span>
    <span>رمز التحقق: <span class="code">{code}</span></span>
  </div>
</div></body></html>"""


def _facts(rows: list[tuple[str, str]]) -> str:
	cells = "".join(
		f'<tr><td class="k">{k}</td><td class="v">{frappe.utils.escape_html(str(v or "—"))}</td></tr>'
		for k, v in rows
	)
	return f'<table class="facts">{cells}</table>'


def _pdf_response(html: str, filename: str):
	"""Send the rendered document as a PDF download."""
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = get_pdf(html, {"orientation": "Portrait"})
	frappe.local.response.type = "pdf"


# --- Documents -------------------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def enrolment_letter(student: str, persona: str = None):
	"""شهادة قيد — proof the student is enrolled."""
	_assert_can_read(persona, student)
	s = _student_brief(student)
	code = _verification_code("enrolment", student)
	year = s["academic_year"] or get_default_academic_year() or ""

	inner = f"""
<div class="body-text center">
  تشهد إدارة المدرسة بأن الطالب/ة <strong>{frappe.utils.escape_html(s['name'])}</strong>
  مُسجّل/ة لدينا في <strong>{frappe.utils.escape_html(s['program'] or '')}</strong>
  للعام الدراسي <strong>{frappe.utils.escape_html(year)}</strong>،
  وأن قيده/ها ساري المفعول حتى تاريخه.
</div>
{_facts([
    ("رقم الطالب", s["id"]),
    ("الصف", s["program"]),
    ("الشعبة", s["batch"]),
    ("تاريخ الميلاد", s["date_of_birth"]),
    ("الجنسية", s["nationality"]),
    ("تاريخ الالتحاق", s["joined"]),
])}
<div class="body-text">
  أُعطيت هذه الشهادة بناءً على طلب ولي الأمر لاستخدامها لدى الجهات الرسمية.
</div>"""

	_pdf_response(_wrap(DOCUMENTS["enrolment"], inner, code), f"شهادة-قيد-{s['name']}.pdf")


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def transcript(student: str, persona: str = None):
	"""كشف علامات — the full academic history, year by year."""
	_assert_can_read(persona, student)

	from match_k12.api.gradebook import academic_record

	result = academic_record(student=student, persona=persona)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result

	if not data or not data.get("periods"):
		return fail(
			message_en="This student has no recorded results yet.",
			message_ar="لا توجد نتائج مسجّلة لهذا الطالب.",
		)
	# The cumulative average is withheld until the administration publishes,
	# and an official transcript must not carry a provisional figure.
	if not data.get("shows_cumulative"):
		return fail(
			message_en="Results are not published yet, so a transcript cannot be issued.",
			message_ar="لم تُنشر النتائج بعد، لا يمكن إصدار كشف علامات رسمي.",
		)

	s = _student_brief(student)
	code = _verification_code("transcript", student)

	blocks = []
	for period in data["periods"]:
		rows = "".join(
			f"<tr><td>{frappe.utils.escape_html(sub['course'])}</td>"
			f'<td class="num">{sub["final"]}</td>'
			f'<td class="num">{sub["grade"]}</td>'
			f"<td>{frappe.utils.escape_html(sub['label'])}</td></tr>"
			for sub in period["subjects"]
		)
		overall = period.get("overall")
		total = (
			f'<tr class="total"><td>معدل الفصل</td>'
			f'<td class="num">{overall}</td>'
			f'<td class="num">{(period.get("overall_grade") or {}).get("grade", "")}</td>'
			f'<td>{frappe.utils.escape_html((period.get("overall_grade") or {}).get("label", ""))}</td></tr>'
			if overall is not None
			else ""
		)
		heading = " — ".join(
			x for x in (period.get("academic_year"), period.get("academic_term")) if x
		)
		blocks.append(
			f'<div class="term-head">{frappe.utils.escape_html(heading)}</div>'
			"<table><thead><tr><th>المادة</th><th>الدرجة</th><th>التقدير</th><th>الوصف</th></tr></thead>"
			f"<tbody>{rows}{total}</tbody></table>"
		)

	cumulative = data.get("cumulative")
	band = data.get("cumulative_grade") or {}
	inner = (
		_facts(
			[
				("اسم الطالب", s["name"]),
				("رقم الطالب", s["id"]),
				("الصف الحالي", s["program"]),
				("تاريخ الميلاد", s["date_of_birth"]),
			]
		)
		+ "".join(blocks)
		+ f"""
<div class="term-head" style="margin-top:20px">النتيجة التراكمية</div>
<table><tbody>
  <tr class="total"><td>المعدل التراكمي</td>
    <td class="num">{cumulative}</td>
    <td class="num">{band.get('grade', '')}</td>
    <td>{frappe.utils.escape_html(band.get('label', ''))}</td></tr>
</tbody></table>"""
	)

	_pdf_response(_wrap(DOCUMENTS["transcript"], inner, code), f"كشف-علامات-{s['name']}.pdf")


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def conduct_certificate(student: str, persona: str = None):
	"""شهادة حسن سيرة وسلوك — drawn from the behaviour record, not asserted."""
	_assert_can_read(persona, student)
	s = _student_brief(student)
	code = _verification_code("conduct", student)

	records = frappe.get_all(
		"K12 Behaviour Record",
		filters={"student": student},
		fields=["record_type", "points"],
		limit=500,
	)
	positive = sum(1 for r in records if r.record_type == "Positive")
	negative = sum(1 for r in records if r.record_type == "Negative")
	net = sum(flt(r.points) for r in records)

	# The wording follows the record rather than blanket-certifying good conduct.
	if negative == 0:
		verdict = "ممتاز — لم تُسجّل أي مخالفة سلوكية"
	elif net >= 0:
		verdict = "جيد — الرصيد السلوكي إيجابي"
	else:
		verdict = "مقبول — توجد ملاحظات سلوكية مسجّلة"

	attendance = frappe.get_all(
		"Student Attendance",
		filters={"student": student, "docstatus": 1},
		fields=["status"],
		limit=2000,
	)
	present = sum(1 for a in attendance if a.status == "Present")
	rate = round(present / len(attendance) * 100, 1) if attendance else 0

	inner = f"""
<div class="body-text center">
  تشهد إدارة المدرسة بأن الطالب/ة <strong>{frappe.utils.escape_html(s['name'])}</strong>
  قد أظهر/ت سلوكاً <strong>{frappe.utils.escape_html(verdict)}</strong>
  خلال فترة دراسته/ها في المدرسة.
</div>
{_facts([
    ("رقم الطالب", s["id"]),
    ("الصف", s["program"]),
    ("المواقف الإيجابية المسجّلة", positive),
    ("الملاحظات السلوكية", negative),
    ("نسبة الحضور", f"{rate}%"),
])}
<div class="body-text">
  أُعطيت هذه الشهادة بناءً على طلب ولي الأمر دون أدنى مسؤولية على المدرسة.
</div>"""

	_pdf_response(_wrap(DOCUMENTS["conduct"], inner, code), f"شهادة-سلوك-{s['name']}.pdf")


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def graduation_certificate(student: str, persona: str = None):
	"""شهادة تخرج — issued by the administration only."""
	s = _student_brief(student)
	code = _verification_code("graduation", student)

	from match_k12.api.gradebook import academic_record

	result = academic_record(student=student, persona=persona)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result
	cumulative = (data or {}).get("cumulative")
	band = (data or {}).get("cumulative_grade") or {}

	if cumulative is None:
		return fail(
			message_en="A graduation certificate needs published results.",
			message_ar="لا يمكن إصدار شهادة تخرج قبل نشر النتائج النهائية.",
		)

	inner = f"""
<div class="body-text center" style="font-size:14px">
  تشهد إدارة المدرسة بأن الطالب/ة
  <br><strong style="font-size:19px">{frappe.utils.escape_html(s['name'])}</strong><br>
  قد أتمّ/ت بنجاح متطلبات <strong>{frappe.utils.escape_html(s['program'] or '')}</strong>
  للعام الدراسي <strong>{frappe.utils.escape_html(s['academic_year'] or '')}</strong>
  بمعدل تراكمي <strong class="num">{cumulative}%</strong>
  وتقدير <strong>{frappe.utils.escape_html(band.get('label', ''))}</strong>.
</div>
{_facts([
    ("رقم الطالب", s["id"]),
    ("تاريخ الميلاد", s["date_of_birth"]),
    ("الجنسية", s["nationality"]),
    ("تاريخ الالتحاق", s["joined"]),
])}
<div class="body-text center">
  وقد مُنح/ت هذه الشهادة تقديراً لجهوده/ها، متمنّين له/ها التوفيق والنجاح.
</div>"""

	_pdf_response(_wrap(DOCUMENTS["graduation"], inner, code), f"شهادة-تخرج-{s['name']}.pdf")


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def available_documents(student: str = None, persona: str = None):
	"""Which documents this viewer may request, and whether each is issuable."""
	scope = resolve_scope(persona)
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		students = scope.get("students") or []
		student = student if student in students else (students[0] if students else None)
	if not student:
		return {"student": None, "documents": []}

	_assert_can_read(persona, student)

	from match_k12.api.gradebook import academic_record

	result = academic_record(student=student, persona=persona)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result
	published = bool((data or {}).get("shows_cumulative"))
	back_office = persona in BACK_OFFICE

	return {
		"student": student,
		"student_name": _student_brief(student)["name"],
		"documents": [
			{
				"kind": "enrolment",
				"label": DOCUMENTS["enrolment"],
				"description": "إثبات أن الطالب مُسجّل في المدرسة هذا العام",
				"available": True,
				"reason": None,
			},
			{
				"kind": "transcript",
				"label": DOCUMENTS["transcript"],
				"description": "كشف كامل بالعلامات لكل سنة وفصل مع المعدل التراكمي",
				"available": published,
				"reason": None if published else "بانتظار نشر النتائج من الإدارة",
			},
			{
				"kind": "conduct",
				"label": DOCUMENTS["conduct"],
				"description": "شهادة سلوك مبنية على السجل السلوكي والحضور",
				"available": True,
				"reason": None,
			},
			{
				"kind": "graduation",
				"label": DOCUMENTS["graduation"],
				"description": "تُصدر من الإدارة بعد إتمام متطلبات الصف النهائي",
				"available": back_office and published,
				"reason": (
					None
					if back_office and published
					else "تُصدر من الإدارة بعد نشر النتائج"
				),
			},
		],
	}
