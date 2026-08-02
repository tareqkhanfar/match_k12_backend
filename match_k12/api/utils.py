# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Shared helpers for the Match K12 API layer.

Every endpoint returns a flat envelope so the frontend can rely on one shape:

    {"success": bool, "data": Any, "message_en": str, "message_ar": str}
"""

import functools
from typing import Any

import frappe
from frappe import _


# --- Roles -----------------------------------------------------------------
# The frontend has exactly four personas. Each maps to a Frappe role that this
# app creates on install (see match_k12/setup/install.py).

ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLE_PARENT = "parent"

FRAPPE_ROLE_BY_PERSONA = {
	ROLE_ADMIN: "K12 School Admin",
	ROLE_TEACHER: "K12 Teacher",
	ROLE_STUDENT: "K12 Student",
	ROLE_PARENT: "K12 Parent",
}

# Highest privilege first — a user holding several roles resolves to the first
# match, so an admin who is also a teacher is treated as an admin.
PERSONA_PRIORITY = (ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)


def ok(data: Any = None, message_en: str = "", message_ar: str = "") -> dict:
	return {
		"success": True,
		"data": data,
		"message_en": message_en,
		"message_ar": message_ar,
	}


def fail(message_en: str = "", message_ar: str = "", data: Any = None) -> dict:
	return {
		"success": False,
		"data": data,
		"message_en": message_en,
		"message_ar": message_ar,
	}


def get_persona(user: str | None = None) -> str | None:
	"""Resolve the frontend persona for a user from their Frappe roles."""
	user = user or frappe.session.user
	if user == "Guest":
		return None

	# System Manager / Administrator always act as the school admin so the
	# system is usable before any K12 role has been handed out.
	roles = set(frappe.get_roles(user))
	if "Administrator" in roles or "System Manager" in roles:
		return ROLE_ADMIN

	for persona in PERSONA_PRIORITY:
		if FRAPPE_ROLE_BY_PERSONA[persona] in roles:
			return persona
	return None


def require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to continue."), frappe.AuthenticationError)


def require_persona(*allowed: str) -> str:
	"""Ensure the caller holds one of `allowed` personas; return the persona."""
	require_login()
	persona = get_persona()
	if persona is None:
		frappe.throw(
			_("Your account is not linked to a Match K12 role."),
			frappe.PermissionError,
		)
	if allowed and persona not in allowed:
		frappe.throw(
			_("You are not allowed to access this resource."),
			frappe.PermissionError,
		)
	return persona


def k12_endpoint(*allowed_personas: str):
	"""Wrap an endpoint: enforce persona access and return the flat envelope.

	The wrapped function receives `persona` as a keyword argument and returns
	plain data; this decorator handles the envelope and error translation.
	"""

	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			persona = require_persona(*allowed_personas)
			kwargs.pop("persona", None)
			data = fn(*args, persona=persona, **kwargs)
			# Endpoints may return a finished envelope themselves.
			if isinstance(data, dict) and "success" in data:
				return data
			return ok(data)

		return wrapper

	return decorator


# --- Linked-record resolution ---------------------------------------------
# Student / Instructor / Guardian each carry a `user` link. These helpers turn
# the logged-in user into the education record that scopes their data.


def get_linked_student(user: str | None = None) -> str | None:
	user = user or frappe.session.user
	return frappe.db.get_value("Student", {"user": user}, "name")


def get_linked_instructor(user: str | None = None) -> str | None:
	"""Instructor links to a User indirectly, through Employee."""
	user = user or frappe.session.user
	employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if employee:
		instructor = frappe.db.get_value("Instructor", {"employee": employee}, "name")
		if instructor:
			return instructor
	# Fall back to matching on the instructor name for setups without HR.
	full_name = frappe.db.get_value("User", user, "full_name")
	if full_name:
		return frappe.db.get_value("Instructor", {"instructor_name": full_name}, "name")
	return None


def get_linked_guardian(user: str | None = None) -> str | None:
	user = user or frappe.session.user
	return frappe.db.get_value("Guardian", {"user": user}, "name")


def get_guardian_students(guardian: str) -> list[str]:
	"""Students linked to a guardian via the Student Guardian child table."""
	if not guardian:
		return []
	return [
		r.parent
		for r in frappe.get_all(
			"Student Guardian",
			filters={"guardian": guardian, "parenttype": "Student"},
			fields=["parent"],
		)
	]


def resolve_scope(persona: str) -> dict:
	"""Return the record ids that scope what this user may read."""
	scope = {
		"persona": persona,
		"student": None,
		"instructor": None,
		"guardian": None,
		"students": [],
	}
	if persona == ROLE_STUDENT:
		student = get_linked_student()
		scope["student"] = student
		scope["students"] = [student] if student else []
	elif persona == ROLE_TEACHER:
		scope["instructor"] = get_linked_instructor()
	elif persona == ROLE_PARENT:
		guardian = get_linked_guardian()
		scope["guardian"] = guardian
		scope["students"] = get_guardian_students(guardian)
	return scope


def get_default_academic_year() -> str | None:
	year = frappe.db.get_single_value("Education Settings", "current_academic_year")
	if year:
		return year
	rows = frappe.get_all("Academic Year", fields=["name"], order_by="year_start_date desc", limit=1)
	return rows[0].name if rows else None


def get_default_academic_term() -> str | None:
	return frappe.db.get_single_value("Education Settings", "current_academic_term")
