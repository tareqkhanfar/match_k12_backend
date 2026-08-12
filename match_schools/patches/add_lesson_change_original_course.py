"""Let a swap exchange the subjects, not only the teachers.

Swapping two periods used to move the teachers and leave the subjects where
they were, so a class that traded its second and fifth periods still read
"maths" at both ends with different names against them. What a school means by
swapping two periods is that the whole lesson moves — subject and teacher
together.

Restoring one needs the subject it had before, and `course` on MS Lesson
Change is a snapshot of the lesson as it is now, not as it was. This adds the
"before" side so an undo can put the subject back.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"MS Lesson Change": [
				{
					"fieldname": "original_course",
					"label": "Original Subject",
					"fieldtype": "Link",
					"options": "Course",
					"insert_after": "original_instructor",
					"read_only": 1,
					"description": "The subject this lesson carried before the change.",
				}
			]
		},
		ignore_validate=True,
	)

	# Existing records never moved a subject, so the lesson still has the one
	# they were created against.
	if frappe.db.has_column("MS Lesson Change", "original_course"):
		frappe.db.sql(
			"""
			UPDATE `tabMS Lesson Change`
			   SET original_course = course
			 WHERE original_course IS NULL OR original_course = ''
			"""
		)
		frappe.db.commit()
