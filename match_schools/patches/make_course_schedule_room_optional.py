"""Stop requiring a room on every lesson.

Education ships `Course Schedule.room` as mandatory. That suits a timetable
written room by room, but the schools this serves put a class in its own room
all day and never record it — so generating a week failed outright with
"[Course Schedule, EDU-CSH-...]: room", and the lessons never reached the
teachers or the students.

A Property Setter rather than an edit to the doctype: Course Schedule belongs
to the education app, and a direct change would be overwritten on its next
update. Nothing is lost for a school that does use rooms — the field is still
there, still linked to Room, and the timetable still refuses to double-book one.
"""

import frappe


def execute():
	meta = frappe.get_meta("Course Schedule")

	for fieldname in ("room", "instructor"):
		field = meta.get_field(fieldname)
		if not field or not field.reqd:
			continue
		frappe.make_property_setter(
			{
				"doctype": "Course Schedule",
				"doctype_or_field": "DocField",
				"fieldname": fieldname,
				"property": "reqd",
				"value": "0",
				"property_type": "Check",
			},
			is_system_generated=False,
		)

	frappe.clear_cache(doctype="Course Schedule")
