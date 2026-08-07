# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Create one demo login per persona and link it to the right record.

Run with:
    bench --site <site> execute match_schools.setup.demo_users.create_demo_users
"""

import frappe

from match_schools.api.utils import FRAPPE_ROLE_BY_PERSONA

DEMO_PASSWORD = "Match@12345"

DEMO_USERS = [
	{"email": "admin@match-edu.ps", "first_name": "مدير", "last_name": "المدرسة", "persona": "admin"},
	{"email": "secretary@match-edu.ps", "first_name": "سكرتارية", "last_name": "المدرسة", "persona": "secretary"},
	{"email": "teacher@match-edu.ps", "first_name": "معلم", "last_name": "تجريبي", "persona": "teacher"},
	{"email": "student@match-edu.ps", "first_name": "طالب", "last_name": "تجريبي", "persona": "student"},
	{"email": "parent@match-edu.ps", "first_name": "ولي", "last_name": "أمر", "persona": "parent"},
]


def create_demo_users():
	created = []
	for spec in DEMO_USERS:
		user = _ensure_user(spec)
		_link_record(user, spec["persona"])
		created.append({"email": spec["email"], "persona": spec["persona"]})

	frappe.db.commit()
	return {"users": created, "password": DEMO_PASSWORD}


def _ensure_user(spec: dict) -> str:
	email = spec["email"]
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": spec["first_name"],
				"last_name": spec["last_name"],
				"send_welcome_email": 0,
				"user_type": "System User",
				"language": "ar",
			}
		)
		user.insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)

	# Assigning only a custom role can flip a user to Website User and
	# disable them, which blocks login — force both back.
	user.enabled = 1
	user.user_type = "System User"

	role = FRAPPE_ROLE_BY_PERSONA[spec["persona"]]
	existing_roles = {r.role for r in user.roles}
	if role not in existing_roles:
		user.append("roles", {"role": role})
	user.save(ignore_permissions=True)

	# Reset the password so the demo credentials always work.
	from frappe.utils.password import update_password

	update_password(email, DEMO_PASSWORD)

	return email


def _link_record(user: str, persona: str):
	"""Point the matching Student / Instructor / Guardian at this user."""
	if persona == "student":
		student = frappe.db.get_value("Student", {"user": user}, "name")
		if not student:
			# Education gives every student its own login from
			# student_email_id, so point the demo account at the first one.
			student = frappe.db.get_value("Student", {"enabled": 1}, "name", order_by="name")
			if student:
				frappe.db.set_value("Student", student, "user", user)

	elif persona == "teacher":
		instructor = frappe.db.get_value("Instructor", {"employee": ["!=", ""]}, "name")
		# Instructor links to a user through Employee; without HR data we
		# fall back to matching the user's full name to the instructor name.
		full_name = frappe.db.get_value("User", user, "full_name")
		target = frappe.db.get_value("Instructor", {"instructor_name": full_name}, "name")
		if not target:
			# Rename the first instructor's display name so the link resolves.
			first = frappe.db.get_value("Instructor", {}, "name")
			if first and full_name:
				frappe.db.set_value("Instructor", first, "instructor_name", full_name)

	elif persona == "parent":
		guardian = frappe.db.get_value("Guardian", {"user": user}, "name")
		if not guardian:
			guardian = _ensure_guardian_for_user(user)
		_attach_children(guardian)


def _ensure_guardian_for_user(user: str) -> str:
	full_name = frappe.db.get_value("User", user, "full_name") or user
	existing = frappe.db.get_value("Guardian", {"guardian_name": full_name}, "name")
	if existing:
		frappe.db.set_value("Guardian", existing, "user", user)
		return existing

	doc = frappe.get_doc(
		{
			"doctype": "Guardian",
			"guardian_name": full_name,
			"email_address": user,
			"user": user,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _attach_children(guardian: str, count: int = 2):
	"""Give the demo parent a couple of children to follow."""
	already = frappe.get_all(
		"Student Guardian",
		filters={"guardian": guardian, "parenttype": "Student"},
		fields=["parent"],
	)
	if len(already) >= count:
		return

	students = frappe.get_all("Student", filters={"enabled": 1}, pluck="name", limit=count)
	for student in students:
		exists = frappe.db.exists(
			"Student Guardian",
			{"parent": student, "parenttype": "Student", "guardian": guardian},
		)
		if exists:
			continue
		doc = frappe.get_doc("Student", student)
		doc.append("guardians", {"guardian": guardian, "relation": "Father"})
		doc.save(ignore_permissions=True)
