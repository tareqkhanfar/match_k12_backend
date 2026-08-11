"""Custom fields the academic-period rules depend on.

`ms_teacher_can_edit_closed_period` decides whether a teacher may still enter
marks or attendance once a term has ended. It is off by default: a closed term
should stay closed unless the school deliberately reopens it for staff.

`Student Attendance.status` gains "Excused" so an absence with a reason can be
told apart from one without. The wording matters — an excused day is not
counted against the student anywhere, so it must be a distinct status rather
than a note on an ordinary absence.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Education Settings": [
				{
					"fieldname": "ms_teacher_can_edit_closed_period",
					"label": "Allow teachers to edit a closed term",
					"fieldtype": "Check",
					"default": "0",
					"insert_after": "current_academic_term",
					"description": (
						"When off, a teacher cannot change attendance or marks once the "
						"term has ended. Admins and secretaries always can."
					),
				}
			]
		},
		ignore_validate=True,
	)

	_extend_attendance_status()
	frappe.db.commit()


def _extend_attendance_status():
	"""Add "Excused" to the attendance status options.

	A Property Setter rather than an edit to the doctype: Student Attendance
	belongs to ERPNext, and a direct change would be undone by the next
	upgrade.
	"""
	meta = frappe.get_meta("Student Attendance")
	field = meta.get_field("status")
	if not field:
		return

	options = [o.strip() for o in (field.options or "").split("\n") if o.strip()]
	if "Excused" in options:
		return

	# Keep "Leave" so historic records stay valid; new entries use "Excused".
	options.append("Excused")
	frappe.make_property_setter(
		{
			"doctype": "Student Attendance",
			"doctype_or_field": "DocField",
			"fieldname": "status",
			"property": "options",
			"value": "\n".join(options),
			"property_type": "Text",
		},
		ignore_validate=True,
	)
