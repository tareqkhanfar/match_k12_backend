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
ROLE_SECRETARY = "secretary"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLE_PARENT = "parent"

FRAPPE_ROLE_BY_PERSONA = {
	ROLE_ADMIN: "K12 School Admin",
	ROLE_SECRETARY: "K12 Secretary",
	ROLE_TEACHER: "K12 Teacher",
	ROLE_STUDENT: "K12 Student",
	ROLE_PARENT: "K12 Parent",
}

PERSONA_LABELS_AR = {
	ROLE_ADMIN: "مدير المدرسة",
	ROLE_SECRETARY: "سكرتارية",
	ROLE_TEACHER: "معلم",
	ROLE_STUDENT: "طالب",
	ROLE_PARENT: "ولي أمر",
}

# Highest privilege first — a user holding several roles resolves to the first
# match, so an admin who is also a teacher is treated as an admin.
PERSONA_PRIORITY = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)

# Personas that manage school-wide records rather than only their own scope.
BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)


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


# --- List query helpers ----------------------------------------------------
# Shared by every list endpoint so filtering, sorting and paging behave the
# same way everywhere (and so the frontend table component can stay generic).


def parse_json_arg(value, default=None):
	"""Accept a value that may arrive as a JSON string over HTTP."""
	if value is None or value == "":
		return default
	if isinstance(value, str):
		try:
			return frappe.parse_json(value)
		except Exception:
			return default
	return value


def build_conditions(
	filters: dict | None,
	allowed: dict[str, str],
	params: dict,
	alias: str = "",
) -> list[str]:
	"""Turn a {field: value} dict into safe SQL conditions.

	`allowed` maps an incoming filter name to its column, which both whitelists
	the field and prevents injection through column names. Values are always
	bound as parameters.

	A value may be a scalar (equality), or a [operator, operand] pair using one
	of: like, in, between, >, <, >=, <=, !=.
	"""
	conditions: list[str] = []
	filters = filters or {}
	prefix = f"{alias}." if alias else ""

	for key, raw in filters.items():
		if key not in allowed or raw in (None, "", []):
			continue
		column = f"{prefix}{allowed[key]}"
		token = f"flt_{len(params)}"

		if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(raw[0], str):
			operator, operand = raw[0].lower(), raw[1]
			if operator == "like":
				conditions.append(f"{column} LIKE %({token})s")
				params[token] = f"%{operand}%"
			elif operator == "in" and operand:
				conditions.append(f"{column} IN %({token})s")
				params[token] = list(operand)
			elif operator == "between" and isinstance(operand, (list, tuple)) and len(operand) == 2:
				lo, hi = f"{token}_lo", f"{token}_hi"
				conditions.append(f"{column} BETWEEN %({lo})s AND %({hi})s")
				params[lo], params[hi] = operand
			elif operator in (">", "<", ">=", "<=", "!="):
				conditions.append(f"{column} {operator} %({token})s")
				params[token] = operand
		else:
			conditions.append(f"{column} = %({token})s")
			params[token] = raw

	return conditions


def build_order_by(
	sort_field: str | None,
	sort_order: str | None,
	allowed: dict[str, str],
	default: str,
	alias: str = "",
) -> str:
	"""Validate sorting against the same whitelist used for filtering."""
	if not sort_field or sort_field not in allowed:
		return default
	direction = "DESC" if str(sort_order).lower() in ("desc", "descending") else "ASC"
	prefix = f"{alias}." if alias else ""
	return f"{prefix}{allowed[sort_field]} {direction}"


def paginate(page, page_size, max_size: int = 200) -> tuple[int, int, int]:
	"""Return (page, page_size, offset) with sane bounds."""
	from frappe.utils import cint

	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 20, 1), max_size)
	return page, page_size, (page - 1) * page_size


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
