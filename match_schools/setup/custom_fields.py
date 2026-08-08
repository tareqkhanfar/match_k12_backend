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
	],
	"Guardian": [
		dict(ID_NUMBER, reqd=0, insert_after="guardian_name"),
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
