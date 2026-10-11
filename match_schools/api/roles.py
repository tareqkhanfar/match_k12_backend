"""What a staff role sees of itself — for the screens that greet it.

The policy lives in `utils.role_policy`; this only answers "who am I here":
the role, and for a supervisor the sections and grades they watch.
"""

import frappe

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_LABELS_AR,
	ROLE_SECRETARY,
	ROLE_SUPERVISOR,
	ROLE_TEACHER,
	get_role,
	ms_endpoint,
	parse_json_arg,
	supervised_groups,
)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def my_scope(persona: str = None):
	"""The caller's role, and a supervisor's sections with their pupils' count."""
	role = get_role()
	out = {"role": role, "roleLabel": ROLE_LABELS_AR.get(role or "", ""), "sections": [], "programs": []}
	if role != ROLE_SUPERVISOR:
		return out
	groups = supervised_groups()
	if groups:
		rows = frappe.get_all(
			"Student Group",
			filters={"name": ["in", groups]},
			fields=["name", "student_group_name", "program", "ms_homeroom_instructor"],
			order_by="program asc, student_group_name asc",
		)
		counts = dict(
			frappe.db.sql(
				"""SELECT parent, COUNT(*) FROM `tabStudent Group Student`
				    WHERE parenttype = 'Student Group' AND active = 1 AND parent IN %(g)s
				    GROUP BY parent""",
				{"g": groups},
			)
		)
		homerooms = dict(
			frappe.get_all(
				"Instructor",
				filters={"name": ["in", [r.ms_homeroom_instructor for r in rows if r.ms_homeroom_instructor] or [""]]},
				fields=["name", "instructor_name"],
				as_list=True,
			)
		)
		out["sections"] = [
			{
				"id": r.name,
				"label": r.student_group_name or r.name,
				"program": r.program or "",
				"students": int(counts.get(r.name, 0)),
				"homeroom": homerooms.get(r.ms_homeroom_instructor, ""),
			}
			for r in rows
		]
	row = frappe.db.get_value("MS Staff Member", {"user": frappe.session.user}, "supervised_programs")
	out["programs"] = parse_json_arg(row, []) or []
	return out
