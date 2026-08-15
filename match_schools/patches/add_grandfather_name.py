"""Give a student the fourth name Arabic schools actually use.

A name here is أربعة أسماء: given, father, grandfather, family. Education
ships three fields and joins them into `student_name`, so the grandfather's
name had nowhere to live and every certificate, register and report printed a
three-part name that does not match the child's identity papers.

`ms_grandfather_name` sits between the father's and the family name — where it
belongs when the name is read aloud — and the controller override in
`hooks.py` rebuilds `student_name` from all four.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	field = {
		"fieldname": "ms_grandfather_name",
		"label": "اسم الجد",
		"fieldtype": "Data",
		"insert_after": "middle_name",
		"description": "الاسم الثالث — يظهر بين اسم الأب واسم العائلة.",
	}

	create_custom_fields(
		{"Student": [field], "Student Applicant": [dict(field)]},
		ignore_validate=True,
	)

	# Existing records keep the three-part name they already have: rebuilding
	# them would leave a gap where the grandfather's name is not known yet.
	frappe.clear_cache(doctype="Student")
	frappe.clear_cache(doctype="Student Applicant")
