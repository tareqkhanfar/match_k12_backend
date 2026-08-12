"""Let a school decide who sees a generated timetable, and when.

Generating lessons used to publish them in the same breath: the moment the
back office pressed the button, every student, guardian and teacher saw the
new week — including the drafts built while a section was still being sorted
out. A timetable is worked on for days before it is meant to be read.

`ms_audience` holds who may see the lessons:

    draft     nobody outside the back office (the default)
    teachers  the staff who teach them
    all       teachers, students and guardians

Existing lessons are set to `all`, so nothing already visible disappears when
this lands.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Course Schedule": [
				{
					"fieldname": "ms_audience",
					"label": "Visible to",
					"fieldtype": "Select",
					"options": "draft\nteachers\nall",
					"default": "draft",
					"insert_after": "room",
					"description": (
						"Who may see this lesson. 'draft' keeps it to the back "
						"office while the week is being built."
					),
				},
				{
					"fieldname": "ms_published_on",
					"label": "Published on",
					"fieldtype": "Datetime",
					"insert_after": "ms_audience",
					"read_only": 1,
				},
			]
		},
		ignore_validate=True,
	)

	# Anything already on the calendar was visible before this patch, and
	# hiding it now would look like the timetable had been deleted.
	if frappe.db.has_column("Course Schedule", "ms_audience"):
		frappe.db.sql(
			"""
			UPDATE `tabCourse Schedule`
			   SET ms_audience = 'all'
			 WHERE ms_audience IS NULL OR ms_audience = ''
			"""
		)
		frappe.db.commit()
