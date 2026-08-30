# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Shared helpers for the Match Schools API layer.

Every endpoint returns a flat envelope so the frontend can rely on one shape:

    {"success": bool, "data": Any, "message_en": str, "message_ar": str}
"""

import functools
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint


# --- Roles -----------------------------------------------------------------
# The frontend has exactly four personas. Each maps to a Frappe role that this
# app creates on install (see match_schools/setup/install.py).

ROLE_ADMIN = "admin"
ROLE_SECRETARY = "secretary"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLE_PARENT = "parent"

FRAPPE_ROLE_BY_PERSONA = {
	ROLE_ADMIN: "MS School Admin",
	ROLE_SECRETARY: "MS Secretary",
	ROLE_TEACHER: "MS Teacher",
	ROLE_STUDENT: "MS Student",
	ROLE_PARENT: "MS Parent",
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
	# system is usable before any Match Schools role has been handed out.
	roles = set(frappe.get_roles(user))
	if "Administrator" in roles or "System Manager" in roles:
		return ROLE_ADMIN

	for persona in PERSONA_PRIORITY:
		if FRAPPE_ROLE_BY_PERSONA[persona] in roles:
			return persona
	return None


def account_block_reason(user: str | None = None) -> dict | None:
	"""Why this account may not use the portal, or None if it may.

	Being able to sign in is not the same as being enrolled. A student who has
	left, a teacher whose employment ended, a guardian whose children have all
	left — each keeps a Frappe User that authenticates perfectly well, and
	until this check existed each of them walked straight into the portal and
	saw a live view of a school they no longer belong to.

	The Frappe User's own `enabled` flag is handled by the login manager; this
	covers the school's own record behind it.
	"""
	user = user or frappe.session.user
	if user in ("Guest", "Administrator"):
		return None

	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return None

	# Checked by record, not by persona. A leaver often keeps only the stock
	# Frappe "Student" role, which get_persona does not recognise, so gating
	# on the persona let exactly the people this is meant to stop walk past.
	OFF = {
		"en": "This account is no longer active. Please contact the school.",
		"ar": "هذا الحساب غير مفعّل حالياً. الرجاء التواصل مع إدارة المدرسة.",
	}

	student = frappe.db.get_value("Student", {"user": user}, ["name", "enabled"], as_dict=True)
	if student and not cint(student.enabled):
		return OFF

	employee = frappe.db.get_value(
		"Employee", {"user_id": user}, ["name", "status"], as_dict=True
	)
	if employee and employee.status and employee.status != "Active":
		return OFF

	# The Instructor is found the same way the rest of the system finds it —
	# through Employee when there is one, otherwise by matching the user's
	# name. Requiring an Employee here meant that on a school running without
	# HR, where every Instructor has no Employee at all, marking a teacher as
	# Left changed nothing and they kept signing in.
	instructor = get_linked_instructor(user)
	if instructor:
		status = frappe.db.get_value("Instructor", instructor, "status")
		if status and status != "Active":
			return OFF

	guardian = frappe.db.get_value("Guardian", {"user": user}, "name")
	if guardian and not student:
		# A guardian has no status of their own: they are active for as long
		# as one of their children is. Blocking on "no children at all" would
		# also lock out a parent whose record was created before the child was
		# linked, so only an all-inactive roster counts.
		children = frappe.get_all(
			"Student Guardian",
			filters={"guardian": guardian, "parenttype": "Student"},
			pluck="parent",
		)
		if children:
			active = frappe.db.count("Student", {"name": ["in", children], "enabled": 1})
			if not active:
				return {
					"en": "No active student is linked to this guardian.",
					"ar": "لا يوجد طالب مفعّل مرتبط بهذا الحساب. الرجاء التواصل مع إدارة المدرسة.",
				}

	return None


def anchor_term(doc, when=None) -> None:
	"""Stamp a document with the year and term it belongs to.

	Every record that describes something happening inside a school year needs
	this. Without it, last year's rows and this year's are the same list: every
	report that filters by term silently misses them, and questions like "how
	many detentions this term" stop having an answer.

	`when` lets a back-dated record land in the term it actually happened in
	rather than today's. The fields are the custom ones added by the anchoring
	patch, so this is a no-op on a doctype that has neither.
	"""
	if not doc:
		return
	meta = frappe.get_meta(doc.doctype)
	has_year = bool(meta.get_field("ms_academic_year"))
	has_term = bool(meta.get_field("ms_academic_term"))
	if not has_year and not has_term:
		return

	year = term = None
	if when:
		day = frappe.utils.getdate(when)
		row = frappe.db.sql(
			"""
			select name, academic_year from `tabAcademic Term`
			where term_start_date <= %(d)s and term_end_date >= %(d)s
			order by term_start_date desc limit 1
			""",
			{"d": day},
			as_dict=True,
		)
		if row:
			term, year = row[0].name, row[0].academic_year

	if has_year and not doc.get("ms_academic_year"):
		doc.ms_academic_year = year or get_default_academic_year()
	if has_term and not doc.get("ms_academic_term"):
		doc.ms_academic_term = term or get_default_academic_term()


def require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to continue."), frappe.AuthenticationError)


def require_persona(*allowed: str) -> str:
	"""Ensure the caller holds one of `allowed` personas; return the persona."""
	require_login()
	persona = get_persona()
	if persona is None:
		frappe.throw(
			_("Your account is not linked to a Match Schools role."),
			frappe.PermissionError,
		)
	if allowed and persona not in allowed:
		frappe.throw(
			_("You are not allowed to access this resource."),
			frappe.PermissionError,
		)
	blocked = account_block_reason()
	if blocked:
		frappe.throw(_(blocked["en"]), frappe.PermissionError)
	return persona


def ms_endpoint(*allowed_personas: str):
	"""Wrap an endpoint: enforce persona access and return the flat envelope.

	The wrapped function receives `persona` as a keyword argument and returns
	plain data; this decorator handles the envelope and error translation.
	"""

	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			persona = require_persona(*allowed_personas)
			kwargs.pop("persona", None)
			try:
				data = fn(*args, persona=persona, **kwargs)
			except frappe.ValidationError as e:
				# A rule the document itself enforces — "date of birth cannot be
				# in the future", a mandatory field, a link that does not exist.
				# These are for the user to act on, so they belong in the
				# envelope; letting them escape reaches the UI as a bare 417 and
				# the screen can only say "could not save".
				frappe.db.rollback()
				message = _clean_message(e)
				frappe.clear_messages()
				# The envelope promises both languages. Reading the session's
				# language for both would hand an Arabic reader an English
				# sentence whenever their account is set to English — which
				# most accounts are, because that is Frappe's default.
				return fail(*_both_languages(message))
			except frappe.PermissionError:
				raise
			except Exception:
				# Anything else is a bug rather than user error: log it with a
				# traceback and keep the details out of the response.
				frappe.db.rollback()
				frappe.log_error(frappe.get_traceback(), f"{fn.__module__}.{fn.__name__} failed")
				return fail(
					"Something went wrong. The error has been logged.",
					"حدث خطأ غير متوقع. تم تسجيل الخطأ لمراجعته.",
				)

			# Endpoints may return a finished envelope themselves.
			if isinstance(data, dict) and "success" in data:
				return data
			return ok(data)

		return wrapper

	return decorator



@functools.lru_cache(maxsize=1)
def _ar_dictionary() -> dict:
	"""The app's Arabic translations, English source → Arabic.

	Cached: it is read on the error path, and re-reading a translation file to
	explain a validation failure would make failures the slow case.
	"""
	try:
		from frappe.translate import get_all_translations

		return dict(get_all_translations("ar") or {})
	except Exception:
		return {}


def _both_languages(message: str) -> tuple[str, str]:
	"""One validation message rendered in English and in Arabic.

	The text arrives already translated into whatever the session speaks, so
	the other language is recovered by looking the pair up rather than by
	translating again.
	"""
	table = _ar_dictionary()
	if message in table:
		return message, table[message]

	reverse = {v: k for k, v in table.items()}
	if message in reverse:
		return reverse[message], message

	# Untranslated: the same sentence twice is honest, and better than an
	# empty field the screen would render as a blank error.
	return message, message


def _clean_message(exc: Exception) -> str:
	"""The readable text of a validation error.

	Frappe puts the useful wording in the message log rather than in the
	exception, and wraps it in markup meant for the desk UI.
	"""
	import re

	text = ""
	for entry in reversed(frappe.get_message_log() or []):
		candidate = entry.get("message") if isinstance(entry, dict) else str(entry)
		if candidate:
			text = candidate
			break
	if not text:
		text = str(exc)

	text = re.sub(r"<[^>]+>", " ", text)          # desk markup
	text = re.sub(r"\s+", " ", text).strip()
	return text or "تعذّر إتمام العملية"


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
	"""The Instructor record behind a user account.

	Three routes, most reliable first. `ms_user` is the explicit link and
	survives a rename on either side; Employee is the HR route; matching on the
	name is the last resort for schools that set neither, and is the one that
	quietly breaks — rename a teacher and they lose their classes, or keep
	signing in after being marked Left because nothing can find their record.
	"""
	user = user or frappe.session.user

	direct = frappe.db.get_value("Instructor", {"ms_user": user}, "name")
	if direct:
		return direct

	employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if employee:
		instructor = frappe.db.get_value("Instructor", {"employee": employee}, "name")
		if instructor:
			return instructor

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


# Where a user's chosen period is stored. Defined here rather than imported
# from api.academic_context to avoid a circular import: that module imports
# this one.
_PREF_YEAR = "ms_academic_year"
_PREF_TERM = "ms_academic_term"


def get_default_academic_year() -> str | None:
	"""The academic year every screen should read.

	The user's own choice from the header wins. Almost the whole API already
	calls this helper, so honouring the preference here is what makes the
	header switcher apply system-wide rather than only to the few screens that
	knew to ask for it.
	"""
	chosen = frappe.defaults.get_user_default(_PREF_YEAR) or None
	if chosen and frappe.db.exists("Academic Year", chosen):
		return chosen

	year = frappe.db.get_single_value("Education Settings", "current_academic_year")
	if year:
		return year
	rows = frappe.get_all("Academic Year", fields=["name"], order_by="year_start_date desc", limit=1)
	return rows[0].name if rows else None


def get_default_academic_term() -> str | None:
	"""The academic term every screen should read — the user's choice first.

	A term belonging to a different year than the selected one is ignored: the
	combination would silently filter everything to nothing.
	"""
	year = frappe.defaults.get_user_default(_PREF_YEAR) or None
	chosen = frappe.defaults.get_user_default(_PREF_TERM) or None

	if chosen and frappe.db.exists("Academic Term", chosen):
		if not year or frappe.db.get_value("Academic Term", chosen, "academic_year") == year:
			return chosen

	# A year is selected but its term is not — either none was chosen, or the
	# stored one belongs to a different year. Falling back to the school-wide
	# term here would pair the selected year with a term from another year and
	# quietly filter every screen to nothing, so the selected year's own term
	# is used instead.
	if year:
		rows = frappe.get_all(
			"Academic Term",
			filters={"academic_year": year},
			pluck="name",
			order_by="term_start_date",
			limit=1,
		)
		return rows[0] if rows else None

	return frappe.db.get_single_value("Education Settings", "current_academic_term")


def hhmm(value) -> str:
	"""A lesson time as "08:00".

	Frappe returns a Time column as a timedelta, and its str() drops the
	leading zero — "9:40:00", not "09:40:00". Anything slicing five characters
	off that renders "9:40:" on screen, which is how a correctly generated
	timetable came to show the wrong times to students and teachers.

	Every screen that prints a lesson time goes through here, so they cannot
	drift apart again.
	"""
	text = str(value or "")
	if not text:
		return ""
	parts = text.split(":")
	try:
		return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
	except (ValueError, IndexError):
		return text[:5]


def audience_filter(persona: str) -> dict:
	"""Lesson visibility for a persona, as a `get_all` filter fragment.

	A generated timetable is a draft until someone releases it. The back
	office always sees everything — they are the ones building it. Teachers
	see a week released to them or to everyone. Students and guardians see
	only what has been released to all.

	Returned as a filter rather than checked per row so a class's whole week
	is excluded by the query, not filtered afterwards.
	"""
	if persona in BACK_OFFICE:
		return {}
	if persona == ROLE_TEACHER:
		return {"ms_audience": ["in", ["teachers", "all"]]}
	return {"ms_audience": "all"}


def apply_period(
	filters: dict,
	doctype: str,
	academic_year: str | None = None,
	academic_term: str | None = None,
	by_term: bool = True,
) -> dict:
	"""Narrow a query to the period the user has selected in the header.

	Screens that skip this show every year's records at once — last year's
	assignments among this year's, a behaviour count that never resets. The
	caller may pass an explicit year or term (a report over a chosen term);
	otherwise the header's selection applies.

	A record whose term is blank is kept: some records belong to a whole year
	rather than to one term, and dropping them would make a screen emptier
	than the data actually is.
	"""
	meta = frappe.get_meta(doctype)

	if meta.has_field("academic_year"):
		year = academic_year or get_default_academic_year()
		if year:
			filters["academic_year"] = year

	if by_term and meta.has_field("academic_term"):
		term = academic_term or get_default_academic_term()
		if term:
			filters["academic_term"] = ["in", [term, "", None]]

	return filters


def period_conditions(
	doctype: str,
	conditions: list,
	params: dict,
	by_term: bool = True,
	academic_year: str | None = None,
	academic_term: str | None = None,
) -> None:
	"""`apply_period` for the screens that build their WHERE clause by hand.

	Appends to `conditions` and `params` in place. A blank term is kept for
	the same reason as in `apply_period`: some records span the whole year.
	"""
	meta = frappe.get_meta(doctype)

	if meta.has_field("academic_year"):
		year = academic_year or get_default_academic_year()
		if year:
			conditions.append("academic_year = %(ms_period_year)s")
			params["ms_period_year"] = year

	if by_term and meta.has_field("academic_term"):
		term = academic_term or get_default_academic_term()
		if term:
			conditions.append("ifnull(academic_term, '') IN (%(ms_period_term)s, '')")
			params["ms_period_term"] = term
