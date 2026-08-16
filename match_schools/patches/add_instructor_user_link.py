"""Link an Instructor to their User directly.

Instructor has no user field. The system found a teacher's record through
Employee, and when there was no Employee — which is every teacher on a school
running without the HR module — it fell back to matching `User.full_name`
against `Instructor.instructor_name`.

That fallback works until it doesn't. Renaming either side silently breaks the
link: the teacher stops seeing their own classes, or worse, keeps signing in
after being marked Left because the check can no longer find their record.
Two teachers who share a name are ambiguous from the start.

`ms_user` makes the link explicit, the way Student and Guardian already do it.
Existing rows are backfilled from whichever route currently resolves them, so
nothing has to be re-linked by hand; the name fallback stays as a last resort
for records this cannot resolve.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Instructor": [
				{
					"fieldname": "ms_user",
					"label": "User account",
					"fieldtype": "Link",
					"options": "User",
					"insert_after": "instructor_name",
					"description": (
						"The portal account for this teacher. Set it and the link "
						"survives a rename on either side."
					),
				},
			],
		},
		ignore_validate=True,
	)

	# Backfill from the two routes that resolve a teacher today, so no school
	# has to re-link anyone by hand.
	filled = 0
	for row in frappe.get_all(
		"Instructor",
		filters={"ms_user": ["in", ["", None]]},
		fields=["name", "instructor_name", "employee"],
		limit_page_length=0,
	):
		user = None
		if row.employee:
			user = frappe.db.get_value("Employee", row.employee, "user_id")
		if not user and row.instructor_name:
			# Only when the name resolves to exactly one enabled account —
			# guessing between two would attach a teacher to the wrong person.
			matches = frappe.get_all(
				"User",
				filters={"full_name": row.instructor_name, "enabled": 1},
				pluck="name",
				limit=2,
			)
			if len(matches) == 1:
				user = matches[0]
		if user:
			frappe.db.set_value("Instructor", row.name, "ms_user", user, update_modified=False)
			filled += 1

	frappe.db.commit()
	print(f"Linked {filled} instructor(s) to a user account.")
