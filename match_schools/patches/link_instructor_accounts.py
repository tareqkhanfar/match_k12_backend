"""Make every teacher's login link explicit.

Accounts issued before the bulk tool wrote `Instructor.ms_user` were found
only by matching the teacher's name to the account's name — which breaks on a
rename and could pick a stranger who shares the name. Where exactly one
enabled teacher account carries the name and no other instructor claims it,
the link is written down; anything ambiguous is left for the office.
"""

import frappe


def execute():
	if not frappe.db.has_column("Instructor", "ms_user"):
		return
	from match_schools.api.credentials import _unclaimed_teacher_by_name

	for name in frappe.get_all("Instructor", filters={"ms_user": ["is", "not set"]}, pluck="name"):
		employee = frappe.db.get_value("Instructor", name, "employee")
		user = frappe.db.get_value("Employee", employee, "user_id") if employee else None
		user = user or _unclaimed_teacher_by_name(name)
		if user and not frappe.db.exists("Instructor", {"ms_user": user}):
			frappe.db.set_value("Instructor", name, "ms_user", user, update_modified=False)
