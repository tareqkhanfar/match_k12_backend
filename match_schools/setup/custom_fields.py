"""Custom fields this app adds to the Education doctypes.

Kept as custom fields rather than edits to the education app so that app can
still be updated from upstream.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# رقم الهوية — the national id. Needed on the applicant so it is captured at
# registration, and carried onto the Student record it becomes.
ID_NUMBER = {
	"fieldname": "ms_id_number",
	"label": "رقم الهوية",
	"fieldtype": "Data",
	"insert_after": "last_name",
	"unique": 0,  # duplicates are rejected in code, with a clearer message
	"translatable": 0,
}

CUSTOM_FIELDS = {
	"Student Applicant": [
		dict(ID_NUMBER, reqd=1),
		{
			"fieldname": "ms_username",
			"label": "اسم المستخدم",
			"fieldtype": "Data",
			"insert_after": "student_email_id",
			"read_only": 1,
			"no_copy": 1,
			"translatable": 0,
		},
	],
	"Student": [
		dict(ID_NUMBER, reqd=0),
	],
	"Instructor": [
		dict(ID_NUMBER, reqd=0, insert_after="instructor_name"),
		{
			"fieldname": "ms_weekly_quota",
			"label": "نصاب الحصص الأسبوعي",
			"fieldtype": "Int",
			"insert_after": "status",
			"description": (
				"أقصى عدد حصص أسبوعية لهذا المعلم. يمنع بناء الجدول من تجاوزه، "
				"ويُترك صفراً حين لا نصاب محدّد."
			),
			"translatable": 0,
		},
	],
	"Guardian": [
		dict(ID_NUMBER, reqd=0, insert_after="guardian_name"),
	],
	"Education Settings": [
		{
			"fieldname": "ms_force_password_change",
			"label": "إلزام تغيير كلمة المرور عند أول دخول",
			"fieldtype": "Check",
			"insert_after": "user_creation_skip",
			"default": "1",
			"description": (
				"عند التفعيل، يُطلب من كل حساب جديد تغيير كلمة المرور المطبوعة "
				"في أول تسجيل دخول."
			),
			"translatable": 0,
		},
	],
	# Accounts issued by the school start with a generated password printed on
	# a slip. Forcing a change on first use means that printed password stops
	# being a working credential the moment the family has logged in once.
	"User": [
		{
			"fieldname": "ms_must_change_password",
			"label": "Must change password at next login",
			"fieldtype": "Check",
			"insert_after": "user_type",
			"no_copy": 1,
			"translatable": 0,
		},
	],
	# v16 bills school fees as Sales Invoices, but Education only adds `student`
	# and `fee_schedule` — there is no link to the enrolment the fee belongs to.
	# The old `Fees` doctype made that link mandatory, and losing it would mean
	# an invoice that cannot be tied to a programme, year or term. So it is
	# added here, and enforced in `ms_billing.validate_student_invoice`.
	"Sales Invoice": [
		{
			"fieldname": "ms_program_enrollment",
			"label": "التسجيل الدراسي",
			"fieldtype": "Link",
			"options": "Program Enrollment",
			"insert_after": "student",
			"translatable": 0,
			# Mandatory only when the invoice names a student, so ordinary
			# (non-school) sales invoices are untouched.
			"mandatory_depends_on": "eval:!!doc.student",
			"depends_on": "eval:!!doc.student",
		},
		{
			"fieldname": "ms_academic_year",
			"label": "العام الدراسي",
			"fieldtype": "Link",
			"options": "Academic Year",
			"insert_after": "ms_program_enrollment",
			"read_only": 1,
			"fetch_from": "ms_program_enrollment.academic_year",
			"translatable": 0,
			"depends_on": "eval:!!doc.student",
		},
		{
			"fieldname": "ms_academic_term",
			"label": "الفصل الدراسي",
			"fieldtype": "Link",
			"options": "Academic Term",
			"insert_after": "ms_academic_year",
			"read_only": 1,
			"fetch_from": "ms_program_enrollment.academic_term",
			"translatable": 0,
			"depends_on": "eval:!!doc.student",
		},
		{
			"fieldname": "ms_program",
			"label": "البرنامج",
			"fieldtype": "Link",
			"options": "Program",
			"insert_after": "ms_academic_term",
			"read_only": 1,
			"fetch_from": "ms_program_enrollment.program",
			"translatable": 0,
			"depends_on": "eval:!!doc.student",
		},
	],
}


def create_fields():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def remove_fields():
	"""Used when uninstalling."""
	for doctype, fields in CUSTOM_FIELDS.items():
		for field in fields:
			name = "{}-{}".format(doctype, field["fieldname"])
			if frappe.db.exists("Custom Field", name):
				frappe.delete_doc("Custom Field", name, ignore_permissions=True)
