
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Printing that follows the desk.

Documents are rendered with `frappe.get_print`, the same call the desk uses,
so a Print Format edited in ERPNext — or set as the doctype's default, or
picked per request — comes out of this app identically. Nothing about the
layout is duplicated here; change it in the desk and the change appears.

The one thing the desk cannot print is the credentials slip, because a
password exists in readable form only in the response that created it. That
part is appended to the rendered document rather than replacing it, so the
school's own layout is still what the family receives.
"""

import frappe
from frappe.utils import escape_html
from frappe.utils.pdf import get_pdf

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	fail,
	ms_endpoint,
	parse_json_arg,
)

# Printing is only ever allowed for these doctypes, whatever is passed in.
PRINTABLE = {
	"Student Applicant",
	"Student",
	"Fees",
	"Guardian",
	"Instructor",
	"Program Enrollment",
	"Student Attendance",
	"Assessment Result",
}

CREDENTIALS_CSS = """
.ms-cred { border: 2px dashed #4f46e5; border-radius: 10px; padding: 14px 18px;
           margin-top: 18px; font-family: "Cairo","Tahoma",sans-serif;
           direction: rtl; text-align: right; page-break-inside: avoid; }
.ms-cred h3 { margin: 0 0 10px; font-size: 14px; color: #4f46e5; }
.ms-cred .who { font-weight: 700; margin-top: 12px; font-size: 12px; }
.ms-cred table { width: 100%; border-collapse: collapse; margin-top: 4px; }
.ms-cred td { border: none; padding: 4px 0; font-size: 12px; }
.ms-cred .k { color: #6b7280; width: 130px; }
.ms-cred .v { font-family: monospace; font-size: 14px; font-weight: 700;
              direction: ltr; unicode-bidi: isolate; text-align: right; }
.ms-warn { margin-top: 12px; background: #fff7ed; border: 1px solid #fdba74;
           border-radius: 8px; padding: 9px 12px; font-size: 10px; color: #9a3412; }
"""


def _credentials_html(student: dict | None, guardians: list) -> str:
	"""The credentials block appended below the printed document."""
	blocks = []

	def rows(entry: dict, role: str) -> str:
		return (
			'<div class="who">{} — {}</div>'
			"<table>"
			'<tr><td class="k">اسم المستخدم</td><td class="v">{}</td></tr>'
			'<tr><td class="k">كلمة المرور</td><td class="v">{}</td></tr>'
			"</table>"
		).format(
			escape_html(str(entry.get("name") or "")),
			escape_html(role),
			escape_html(str(entry.get("username") or "")),
			escape_html(str(entry.get("password") or "—")),
		)

	if student and student.get("username"):
		blocks.append(rows(student, "الطالب"))
	for g in guardians or []:
		if g and g.get("username"):
			blocks.append(rows(g, "ولي الأمر"))

	if not blocks:
		return ""

	return (
		'<style>{css}</style><div class="ms-cred"><h3>بيانات الدخول</h3>{body}'
		'<div class="ms-warn">⚠️ احتفظ بهذه البيانات في مكان آمن. لن تتمكن المدرسة من عرض '
		"كلمة المرور مرة أخرى — يمكن إصدار كلمة مرور جديدة عند الحاجة.</div></div>"
	).format(css=CREDENTIALS_CSS, body="".join(blocks))


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def print_formats(doctype: str, persona: str = None):
	"""The print formats the desk offers for a doctype, and which is default.

	Lets the screen present the same choice ERPNext does instead of hard-coding
	one layout.
	"""
	if doctype not in PRINTABLE:
		return fail("Not printable", "لا يمكن طباعة هذا المستند")

	formats = frappe.get_all(
		"Print Format",
		filters={"doc_type": doctype, "disabled": 0},
		fields=["name", "standard"],
		order_by="name",
	)
	default = frappe.db.get_value("DocType", doctype, "default_print_format")
	return {
		# "Standard" is always available even with no Print Format rows.
		"formats": ["Standard"] + [f.name for f in formats if f.name != "Standard"],
		"default": default or "Standard",
		"letterheads": frappe.get_all(
			"Letter Head", filters={"disabled": 0}, pluck="name", order_by="name"
		),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def print_document(
	doctype: str,
	name: str,
	print_format: str = None,
	letterhead: str = None,
	language: str = None,
	credentials: str | dict = None,
	guardians: str | list = None,
	persona: str = None,
):
	"""Render a document exactly as the desk would, as a PDF download.

	`print_format` selects among what the desk offers; omitting it uses the
	doctype's default, which is what "the same result as ERPNext" means.
	"""
	return _render(
		doctype, name, print_format, letterhead, language, credentials, guardians, persona
	)


def _render(
	doctype: str,
	name: str,
	print_format: str | None,
	letterhead: str | None,
	language: str | None,
	credentials,
	guardians,
	persona: str,
):
	"""Shared by both endpoints; plain so neither double-wraps the envelope."""
	if doctype not in PRINTABLE:
		return fail("Not printable", "لا يمكن طباعة هذا المستند")
	if not frappe.db.exists(doctype, name):
		return fail("Document not found", "لم يتم العثور على المستند")

	# A family may only print their own paperwork.
	_assert_may_print(doctype, name, persona)

	html = frappe.get_print(
		doctype,
		name,
		print_format=print_format or None,
		letterhead=letterhead or None,
		no_letterhead=0 if letterhead else 1,
		**({"language": language} if language else {}),
	)

	extra = _credentials_html(parse_json_arg(credentials), parse_json_arg(guardians) or [])
	if extra:
		# Appended after the document's own markup so the school's layout is
		# untouched by this app.
		html = "{}{}".format(html, extra)

	frappe.local.response.filename = "{}-{}.pdf".format(doctype, name).replace(" ", "-")
	frappe.local.response.filecontent = get_pdf(html)
	frappe.local.response.type = "pdf"


def _assert_may_print(doctype: str, name: str, persona: str):
	"""Back office prints anything; a family prints only its own records."""
	if persona in (ROLE_ADMIN, ROLE_SECRETARY):
		return

	from match_schools.api.utils import resolve_scope

	scope = resolve_scope(persona)
	allowed = set(filter(None, [scope.get("student")] + list(scope.get("students") or [])))

	if doctype == "Student" and name in allowed:
		return
	if doctype in ("Fees", "Program Enrollment", "Student Attendance", "Assessment Result"):
		if frappe.db.get_value(doctype, name, "student") in allowed:
			return

	frappe.throw(
		"You are not allowed to print this document",
		frappe.PermissionError,
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def registration_slip(
	student: str = None,
	applicant: str = None,
	credentials: str | dict = None,
	guardians: str | list = None,
	print_format: str = None,
	letterhead: str = None,
	persona: str = None,
):
	"""The admission paperwork: the desk's own layout plus the credentials."""
	if student and frappe.db.exists("Student", student):
		doctype, name = "Student", student
	elif applicant and frappe.db.exists("Student Applicant", applicant):
		doctype, name = "Student Applicant", applicant
	else:
		return fail("Student or applicant not found", "لم يتم العثور على الطالب أو الطلب")

	return _render(
		doctype, name, print_format, letterhead, None, credentials, guardians, persona
	)
