"""Which audiences each role may write to, as a setting the school controls.

Every school draws these lines differently. One lets teachers write to the
whole staff; another routes everything through the office. One lets students
message their teachers; another allows only guardians to. Hard-coding any of
that means the second school cannot use the system.

The policy is a grid: for each role, which audiences it may address. An
audience is a kind of person, not a list — "the guardians of my classes" is an
audience; the two hundred names behind it are resolved at send time from what
that particular caller may see. So a teacher choosing "guardians of my
classes" reaches their own classes, never another teacher's.

The defaults are what a cautious school would choose: staff can reach
families, families can reach staff, and children cannot reach each other.
"""

import frappe
from match_schools.api.utils import (
	apply_period,
	ms_endpoint,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

POLICY_KEY = "ms_mail_policy"

# Every audience the system understands. `scope` says how the members are
# found: "mine" narrows to the caller's own classes, "all" is school-wide.
AUDIENCES = {
	"my_class_guardians": {
		"label_ar": "أولياء أمور صفوفي",
		"label_en": "Guardians of my classes",
		"scope": "mine",
		"kind": "parent",
	},
	"my_class_students": {
		"label_ar": "طلاب صفوفي",
		"label_en": "Students in my classes",
		"scope": "mine",
		"kind": "student",
	},
	"all_guardians": {
		"label_ar": "جميع أولياء الأمور",
		"label_en": "All guardians",
		"scope": "all",
		"kind": "parent",
	},
	"all_students": {
		"label_ar": "جميع الطلاب",
		"label_en": "All students",
		"scope": "all",
		"kind": "student",
	},
	"all_teachers": {
		"label_ar": "جميع المعلمين",
		"label_en": "All teachers",
		"scope": "all",
		"kind": "teacher",
	},
	"my_teachers": {
		# A parent reads "my children's teachers"; a student reads "my
		# teachers". Same audience, different sentence — labelled per caller
		# in audiences_for rather than duplicated as two audiences.
		"label_ar": "معلّمو صفوفي",
		"label_en": "My teachers",
		"scope": "mine",
		"kind": "teacher",
	},
	"office": {
		"label_ar": "إدارة المدرسة",
		"label_en": "The school office",
		"scope": "all",
		"kind": "office",
	},
}

ROLE_LABELS = {
	ROLE_ADMIN: "مدير المدرسة",
	ROLE_SECRETARY: "السكرتير",
	ROLE_TEACHER: "المعلّم",
	ROLE_STUDENT: "الطالب",
	ROLE_PARENT: "ولي الأمر",
}

# What a cautious school would choose. Children cannot reach one another, and
# a teacher reaches their own classes rather than the whole school.
DEFAULT_POLICY = {
	ROLE_ADMIN: [
		"all_teachers", "all_guardians", "all_students", "office",
		"my_class_guardians", "my_class_students",
	],
	ROLE_SECRETARY: [
		"all_teachers", "all_guardians", "all_students", "office",
		"my_class_guardians", "my_class_students",
	],
	ROLE_TEACHER: ["my_class_guardians", "my_class_students", "all_teachers", "office"],
	ROLE_STUDENT: ["my_teachers", "office"],
	ROLE_PARENT: ["my_teachers", "office"],
}


def get_policy() -> dict[str, list[str]]:
	"""The school's grid, falling back to the defaults."""
	raw = frappe.db.get_default(POLICY_KEY)
	stored = {}
	if raw:
		try:
			stored = frappe.parse_json(raw) or {}
		except Exception:
			stored = {}
	out = {}
	for role, default in DEFAULT_POLICY.items():
		chosen = stored.get(role)
		# An explicit empty list means "this role may write to nobody", which
		# is a legitimate choice; only a missing key falls back.
		out[role] = [a for a in chosen if a in AUDIENCES] if isinstance(chosen, list) else list(default)
	return out


def audiences_for(persona: str) -> list[dict]:
	"""The audiences this role may address, ready for the compose screen."""
	allowed = get_policy().get(persona, [])
	return [
		{
			"key": key,
			"label": (
				"معلّمو أبنائي"
				if key == "my_teachers" and persona == ROLE_PARENT
				else AUDIENCES[key]["label_ar"]
			),
			"scope": AUDIENCES[key]["scope"],
			"kind": AUDIENCES[key]["kind"],
		}
		for key in allowed
		if key in AUDIENCES
	]


def _my_group_ids(persona: str) -> list[str]:
	"""Classes the caller is connected to."""
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return []
		groups = set(
			frappe.get_all(
				"Student Group Instructor",
				filters={"instructor": instructor, "parenttype": "Student Group"},
				pluck="parent",
			)
		)
		groups |= {
			r.student_group
			for r in frappe.get_all(
				"Course Schedule",
				filters={"instructor": instructor, "docstatus": ["<", 2]},
				fields=["student_group"],
				limit_page_length=0,
			)
			if r.student_group
		}
		return sorted(groups)

	students = scope.get("students") or []
	if not students:
		return []
	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", students], "active": 1},
				fields=["parent"],
				limit_page_length=0,
			)
		}
	)


def _office_users() -> list[str]:
	users = set()
	for role in ("MS School Admin", "MS Secretary"):
		users |= {
			r.parent
			for r in frappe.get_all(
				"Has Role", filters={"role": role, "parenttype": "User"}, fields=["parent"]
			)
		}
	enabled = frappe.get_all(
		"User", filters={"name": ["in", list(users) or [""]], "enabled": 1}, pluck="name"
	)
	return sorted(enabled)


def _students_of_groups(groups: list[str]) -> list[str]:
	if not groups:
		return []
	return frappe.get_all(
		"Student Group Student",
		filters={"parent": ["in", groups], "active": 1},
		pluck="student",
	)


def resolve_audience(persona: str, key: str, groups: list[str] | None = None) -> list[str]:
	"""The user accounts behind one audience, for this caller.

	`groups` narrows a "mine" audience further — picking two of a teacher's
	five classes. A class the caller has no connection to is dropped rather
	than refused, so a stale selection cannot widen the reach.
	"""
	if key not in AUDIENCES or key not in get_policy().get(persona, []):
		return []

	spec = AUDIENCES[key]
	kind, scope = spec["kind"], spec["scope"]

	if kind == "office":
		return [u for u in _office_users() if u != frappe.session.user]

	if scope == "mine":
		mine = _my_group_ids(persona)
		chosen = [g for g in (groups or mine) if g in mine] or mine
	else:
		chosen = groups or frappe.get_all("Student Group", filters={"disabled": 0}, pluck="name")

	if kind == "teacher":
		if scope == "all":
			ids = {
				r.parent
				for r in frappe.get_all(
					"Has Role",
					filters={"role": "MS Teacher", "parenttype": "User"},
					fields=["parent"],
				)
			}
			users = frappe.get_all(
				"User", filters={"name": ["in", list(ids) or [""]], "enabled": 1}, pluck="name"
			)
		else:
			instructors = frappe.get_all(
				"Student Group Instructor",
				filters={"parent": ["in", chosen or [""]], "parenttype": "Student Group"},
				pluck="instructor",
			)
			instructors += [
				r.instructor
				for r in frappe.get_all(
					"Course Schedule",
					filters={"student_group": ["in", chosen or [""]], "docstatus": ["<", 2]},
					fields=["instructor"],
					limit_page_length=0,
				)
				if r.instructor
			]
			users = []
			for i in set(instructors):
				employee = frappe.db.get_value("Instructor", i, "employee")
				user = frappe.db.get_value("Employee", employee, "user_id") if employee else None
				if not user:
					user = frappe.db.get_value("Instructor", i, "ms_user")
				if user:
					users.append(user)
		return sorted({u for u in users if u and u != frappe.session.user})

	students = _students_of_groups(chosen)
	if kind == "student":
		users = frappe.get_all(
			"Student",
			filters={"name": ["in", students or [""]], "enabled": 1},
			pluck="user",
		)
		return sorted({u for u in users if u and u != frappe.session.user})

	# Guardians of those students.
	guardians = frappe.get_all(
		"Student Guardian",
		filters={"parent": ["in", students or [""]], "parenttype": "Student"},
		pluck="guardian",
	)
	users = frappe.get_all(
		"Guardian", filters={"name": ["in", list(set(guardians)) or [""]]}, pluck="user"
	)
	return sorted({u for u in users if u and u != frappe.session.user})


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def my_audiences(persona: str = None):
	"""Audiences this caller may address, with the classes behind the narrow ones."""
	rows = audiences_for(persona)
	groups = []
	if any(a["scope"] == "mine" for a in rows) or persona in (ROLE_ADMIN, ROLE_SECRETARY):
		ids = (
			_my_group_ids(persona)
			if persona not in (ROLE_ADMIN, ROLE_SECRETARY)
			else frappe.get_all(
				"Student Group",
				filters=apply_period({"disabled": 0}, "Student Group"),
				pluck="name",
			)
		)
		groups = [
			{
				"id": g,
				"label": frappe.db.get_value("Student Group", g, "student_group_name") or g,
			}
			for g in ids
		]
	return {"audiences": rows, "groups": groups}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def get_settings(persona: str = None):
	"""The whole grid, for the settings screen."""
	policy = get_policy()
	return {
		"roles": [
			{"key": r, "label": ROLE_LABELS[r], "allowed": policy.get(r, [])}
			for r in (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
		],
		"audiences": [
			{"key": k, "label": v["label_ar"], "scope": v["scope"], "kind": v["kind"]}
			for k, v in AUDIENCES.items()
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_settings(payload: str | dict = None, persona: str = None):
	"""Store the grid. Only the head may change who can write to whom."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	policy = data.get("policy") or {}

	clean: dict[str, list[str]] = {}
	for role in DEFAULT_POLICY:
		chosen = policy.get(role)
		if not isinstance(chosen, list):
			clean[role] = list(DEFAULT_POLICY[role])
			continue
		clean[role] = [a for a in chosen if a in AUDIENCES]

	frappe.db.set_default(POLICY_KEY, frappe.as_json(clean))
	frappe.db.commit()
	return {
		"success": True,
		"data": {"saved": True},
		"message_en": "Messaging policy saved.",
		"message_ar": "تم حفظ صلاحيات المراسلة.",
	}


def allowed_users(persona: str) -> set[str]:
	"""Every user this caller may write to under the current policy.

	The single authority for send-time validation: the compose screen offers
	audiences, but what actually leaves is checked against this.
	"""
	out: set[str] = set()
	for key in get_policy().get(persona, []):
		out |= set(resolve_audience(persona, key))
	return out
