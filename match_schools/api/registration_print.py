
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The printable registration slip handed to a family at admission.

It carries the registration details and the login for the student and each
guardian. The password is passed in by the caller rather than read from the
database, because it is only ever readable at the moment it is generated —
this endpoint prints what the admit call just returned.

That also means the slip cannot be reprinted later with the same password. If
a family loses it, issue a new password (`credentials.reset_password`) and
print again, which is the safer behaviour anyway.
"""

import frappe
from frappe.utils import escape_html, format_date, today
from frappe.utils.pdf import get_pdf

from match_schools.api.certificates import PAGE_CSS, _school
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	fail,
	ms_endpoint,
	parse_json_arg,
)

EXTRA_CSS = """
.cred { border: 2px dashed #4f46e5; border-radius: 10px; padding: 14px 18px; margin-top: 18px; }
.cred h3 { margin: 0 0 8px; font-size: 13px; color: #4f46e5; }
.cred .who { font-weight: 700; margin-top: 10px; }
.cred table { margin-top: 6px; }
.cred td { border: none; padding: 4px 0; }
.cred .k { color: #6b7280; width: 120px; }
.cred .v { font-family: monospace; font-size: 14px; font-weight: 700;
           direction: ltr; unicode-bidi: isolate; text-align: right; }
.warn { margin-top: 14px; background: #fff7ed; border: 1px solid #fdba74;
        border-radius: 8px; padding: 9px 12px; font-size: 10px; color: #9a3412; }
"""


def _fact_rows(rows: list[tuple[str, str]]) -> str:
	cells = "".join(
		'<tr><td class="k">{}</td><td class="v">{}</td></tr>'.format(
			escape_html(str(k)), escape_html(str(v or "—"))
		)
		for k, v in rows
	)
	return '<table class="facts">{}</table>'.format(cells)


def _credential_block(title: str, entries: list[dict]) -> str:
	if not entries:
		return ""
	body = []
	for e in entries:
		if not e or not e.get("username"):
			continue
		body.append('<div class="who">{}</div>'.format(escape_html(e.get("name") or "")))
		body.append(
			'<table><tr><td class="k">اسم المستخدم</td><td class="v">{}</td></tr>'
			'<tr><td class="k">كلمة المرور</td><td class="v">{}</td></tr></table>'.format(
				escape_html(e.get("username") or ""),
				escape_html(e.get("password") or "—"),
			)
		)
	if not body:
		return ""
	return '<div class="cred"><h3>{}</h3>{}</div>'.format(escape_html(title), "".join(body))


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def registration_slip(
	student: str = None,
	applicant: str = None,
	credentials: str | dict = None,
	guardians: str | list = None,
	persona: str = None,
):
	"""Render the registration + credentials slip as a PDF download."""
	cred = parse_json_arg(credentials) or {}
	guardian_creds = parse_json_arg(guardians) or []

	if student and frappe.db.exists("Student", student):
		doc = frappe.get_doc("Student", student)
		name = doc.student_name
		facts = [
			("رقم الطالب", doc.name),
			("الاسم", name),
			("رقم الهوية", doc.get("ms_id_number")),
			("تاريخ الميلاد", format_date(doc.date_of_birth, "dd-MM-yyyy") if doc.date_of_birth else ""),
			("الجنس", doc.gender),
			("الجوال", doc.student_mobile_number),
			("تاريخ الالتحاق", format_date(doc.joining_date, "dd-MM-yyyy") if doc.joining_date else ""),
		]
	elif applicant and frappe.db.exists("Student Applicant", applicant):
		doc = frappe.get_doc("Student Applicant", applicant)
		name = doc.title
		facts = [
			("رقم الطلب", doc.name),
			("الاسم", name),
			("رقم الهوية", doc.get("ms_id_number")),
			("البرنامج", doc.program),
			("العام الدراسي", doc.academic_year),
			("تاريخ الطلب", format_date(doc.application_date, "dd-MM-yyyy") if doc.application_date else ""),
		]
	else:
		return fail("Student or applicant not found", "لم يتم العثور على الطالب أو الطلب")

	inner = _fact_rows(facts)
	inner += _credential_block("بيانات دخول الطالب", [cred] if cred else [])
	inner += _credential_block("بيانات دخول أولياء الأمور", guardian_creds)
	if cred or guardian_creds:
		inner += (
			'<div class="warn">⚠️ احتفظ بهذه البيانات في مكان آمن. '
			"لن تتمكن المدرسة من عرض كلمة المرور مرة أخرى — يمكن إصدار كلمة مرور جديدة عند الحاجة.</div>"
		)

	school = _school()
	contact = " • ".join(x for x in (school["phone"], school["email"]) if x)
	html = """<!doctype html><html><head><meta charset="utf-8">
<style>{css}{extra}</style></head><body>
<div class="frame">
  <div class="head">
    <div class="school">{school}</div>
    <div class="contact">{contact}</div>
  </div>
  <div class="center"><div class="doc-title">إشعار تسجيل</div></div>
  {inner}
  <div class="sign">
    <div><div class="line">توقيع ولي الأمر</div></div>
    <div><div class="line">توقيع المسجّل</div></div>
  </div>
  <div class="foot"><span>تاريخ الطباعة: {issued}</span><span class="code">MATCH</span></div>
</div></body></html>""".format(
		css=PAGE_CSS,
		extra=EXTRA_CSS,
		school=escape_html(school["name"]),
		contact=escape_html(contact),
		inner=inner,
		issued=format_date(today(), "dd-MM-yyyy"),
	)

	frappe.local.response.filename = "تسجيل-{}.pdf".format(name or "student")
	frappe.local.response.filecontent = get_pdf(html, {"orientation": "Portrait"})
	frappe.local.response.type = "pdf"
