"""Account creation for the people a school registers.

Every persona gets a login the moment their record is created, with a username
built to a fixed shape and a password generated once:

    <prefix><1><YY><number>        st1260007
     st  student
     t   teacher / instructor
     g   guardian / parent
     sc  secretary
     ad  school admin

`1YY` is a century marker plus the short academic year, so 2026 reads as 126.
`number` is the digits from the record's own id, which keeps the login
traceable back to the row it came from and cannot collide inside a year.

The password is shown to the registrar once, on screen and on the printable
slip, and is never stored in readable form — Frappe keeps only a hash. Losing
it means issuing a new one, which `reset_password` does.
"""

import random
import re
import string

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import update_password

from match_schools.api.utils import (
	FRAPPE_ROLE_BY_PERSONA,
	ROLE_ADMIN,
	fail,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	ms_endpoint,
)

# The login prefix per persona.
PREFIX_BY_PERSONA = {
	ROLE_STUDENT: "st",
	ROLE_TEACHER: "t",
	ROLE_PARENT: "g",
	ROLE_SECRETARY: "sc",
	ROLE_ADMIN: "ad",
}

# Ambiguous characters are left out: a password is read off a printed slip and
# typed by a parent, so 0/O and 1/l/I cause avoidable support calls.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_LENGTH = 8

# Frappe requires an email-shaped User.name; logins are local-part only, so
# they are completed with this domain. Overridable per site.
DEFAULT_LOGIN_DOMAIN = "match-edu.ps"


def login_domain() -> str:
	"""Overridable per site via `ms_login_domain` in site_config.json."""
	return frappe.conf.get("ms_login_domain") or DEFAULT_LOGIN_DOMAIN


def year_token(year: str | int | None = None) -> str:
	"""2026 -> '126'. A leading 1 marks the century, then the short year.

	An Academic Year is usually named for the span it covers ("2025-2026"), so
	the *last* year in the string is the one that identifies the intake.
	"""
	if not year:
		year = frappe.utils.nowdate()[:4]
	digits = re.findall(r"\d{4}", str(year))
	full = digits[-1] if digits else frappe.utils.nowdate()[:4]
	return "1" + full[-2:]


def record_number(docname: str) -> str:
	"""`EDU-APP-2026-00007` -> `0007`.

	The trailing counter, with the year segment dropped so it is not repeated.
	Falls back to whatever digits exist for records named some other way.
	"""
	tail = str(docname).split("-")[-1]
	if tail.isdigit():
		return tail.lstrip("0").zfill(4) if len(tail) > 4 else tail
	digits = re.sub(r"\D", "", str(docname))
	return (digits[-4:] or "0001").zfill(4)


def build_username(persona: str, docname: str, year: str | None = None) -> str:
	prefix = PREFIX_BY_PERSONA.get(persona)
	if not prefix:
		frappe.throw(_("Unknown persona: {0}").format(persona))
	return "{}{}{}".format(prefix, year_token(year), record_number(docname))


def generate_password(length: int = PASSWORD_LENGTH) -> str:
	"""A readable random password.

	`SystemRandom` rather than `random`, because these are real credentials.
	The result is guaranteed to hold a letter and a digit so it satisfies any
	password policy the site has switched on.
	"""
	rng = random.SystemRandom()
	while True:
		pw = "".join(rng.choice(PASSWORD_ALPHABET) for _ in range(length))
		if any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw):
			return pw


def unique_login(username: str) -> str:
	"""Append a suffix if the login is somehow taken."""
	domain = login_domain()
	candidate = "{}@{}".format(username, domain)
	if not frappe.db.exists("User", candidate):
		return candidate
	for n in range(2, 100):
		candidate = "{}-{}@{}".format(username, n, domain)
		if not frappe.db.exists("User", candidate):
			return candidate
	frappe.throw(_("Could not allocate a username for {0}").format(username))


def create_account(
	persona: str,
	docname: str,
	full_name: str,
	*,
	year: str | None = None,
	mobile: str | None = None,
) -> dict:
	"""Create the User for a person and return the credentials, once.

	The returned password is the only time it is readable. Callers hand it
	straight to the registrar; nothing persists it.
	"""
	username = build_username(persona, docname, year)
	login = unique_login(username)
	password = generate_password()

	parts = (full_name or "").strip().split()
	user = frappe.new_doc("User")
	user.email = login
	user.first_name = parts[0] if parts else username
	if len(parts) > 1:
		user.last_name = " ".join(parts[1:])
	user.username = username
	user.send_welcome_email = 0
	user.user_type = "System User"
	if mobile:
		user.mobile_no = mobile
	user.enabled = 1
	user.insert(ignore_permissions=True)

	user.add_roles(FRAPPE_ROLE_BY_PERSONA[persona])
	update_password(user.name, password)

	# The password was printed on a slip, so require a change at first login
	# unless the school has turned that off.
	if _force_change_enabled():
		frappe.db.set_value(
			"User", user.name, "ms_must_change_password", 1, update_modified=False
		)

	return {
		"user": user.name,
		"username": username,
		"password": password,
		"persona": persona,
		"name": full_name,
	}


def _force_change_enabled() -> bool:
	"""Whether new accounts must change their password at first login.

	On by default: a credential that was printed and handed over should stop
	working as soon as the family has used it once.
	"""
	value = frappe.db.get_single_value("Education Settings", "ms_force_password_change")
	if value is None:
		return True
	return bool(cint(value))


def credentials_for(user_id: str) -> dict | None:
	"""The login half of the credentials, for a user that already exists."""
	if not user_id or not frappe.db.exists("User", user_id):
		return None
	row = frappe.db.get_value(
		"User", user_id, ["name", "username", "full_name", "enabled"], as_dict=True
	)
	return {
		"user": row.name,
		"username": row.username or row.name.split("@")[0],
		"password": None,  # never readable after creation
		"name": row.full_name,
		"enabled": bool(row.enabled),
	}


@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def reset_password(user: str, persona: str = None):
	"""Issue a new password for an existing account, shown once."""
	if not frappe.db.exists("User", user):
		frappe.throw(_("User not found"))

	password = generate_password()
	update_password(user, password)
	if _force_change_enabled():
		frappe.db.set_value("User", user, "ms_must_change_password", 1, update_modified=False)

	row = frappe.db.get_value("User", user, ["username", "full_name"], as_dict=True)
	return {
		"user": user,
		"username": row.username or user.split("@")[0],
		"password": password,
		"name": row.full_name,
	}


# The record types that carry a login, and how to reach the User from each.
CREDENTIAL_SOURCES = {
	"Student": (ROLE_STUDENT, "الطالب"),
	"Instructor": (ROLE_TEACHER, "المعلم"),
	"Guardian": (ROLE_PARENT, "ولي الأمر"),
}


def _user_of(doctype: str, name: str) -> str | None:
	"""The User linked to a person's record.

	Student and Guardian hold the link directly. An Instructor reaches it
	through Employee, and falls back to matching on the full name for schools
	that run without the HR module.
	"""
	if doctype in ("Student", "Guardian"):
		return frappe.db.get_value(doctype, name, "user")

	if doctype == "Instructor":
		employee = frappe.db.get_value("Instructor", name, "employee")
		if employee:
			user = frappe.db.get_value("Employee", employee, "user_id")
			if user:
				return user
		full_name = frappe.db.get_value("Instructor", name, "instructor_name")
		if full_name:
			return frappe.db.get_value("User", {"full_name": full_name, "enabled": 1}, "name")
	return None


def _link_user(doctype: str, name: str, user: str):
	"""Record the new User on the person's own record."""
	if doctype in ("Student", "Guardian"):
		frappe.db.set_value(doctype, name, "user", user, update_modified=False)
	elif doctype == "Instructor":
		employee = frappe.db.get_value("Instructor", name, "employee")
		if employee:
			frappe.db.set_value("Employee", employee, "user_id", user, update_modified=False)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def account_for(doctype: str = None, name: str = None, persona: str = None):
	"""The login a person's record carries, if any.

	The password is never returned — it exists in readable form only at the
	moment it is issued. A school that has lost it resets rather than looks
	it up, which is the behaviour a family expects of their own school.
	"""
	if doctype not in CREDENTIAL_SOURCES or not name:
		return fail(
			message_en="Unsupported record type.",
			message_ar="نوع السجل غير مدعوم.",
		)
	if not frappe.db.exists(doctype, name):
		return fail(
			message_en="That record no longer exists.",
			message_ar="هذا السجل لم يعد موجوداً.",
		)

	role, label = CREDENTIAL_SOURCES[doctype]
	user = _user_of(doctype, name)
	if not user:
		return {
			"doctype": doctype,
			"name": name,
			"label": label,
			"hasAccount": False,
			"account": None,
		}

	row = frappe.db.get_value(
		"User", user, ["name", "username", "full_name", "enabled", "last_login"], as_dict=True
	)
	return {
		"doctype": doctype,
		"name": name,
		"label": label,
		"hasAccount": True,
		"account": {
			"user": row.name,
			"username": row.username or row.name.split("@")[0],
			"name": row.full_name,
			"enabled": bool(row.enabled),
			"lastLogin": str(row.last_login or ""),
			"mustChange": bool(
				frappe.db.get_value("User", user, "ms_must_change_password")
			),
		},
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def issue_account(doctype: str = None, name: str = None, persona: str = None):
	"""Create a login for a person who does not have one yet.

	Separate from resetting: creating an account for someone who already has
	one would orphan the first, leaving two logins for one person and no way
	to tell which is live.
	"""
	if doctype not in CREDENTIAL_SOURCES or not name:
		return fail(
			message_en="Unsupported record type.",
			message_ar="نوع السجل غير مدعوم.",
		)
	if not frappe.db.exists(doctype, name):
		return fail(
			message_en="That record no longer exists.",
			message_ar="هذا السجل لم يعد موجوداً.",
		)
	if _user_of(doctype, name):
		return fail(
			message_en="This person already has an account.",
			message_ar="لدى هذا الشخص حساب بالفعل — استخدم إعادة تعيين كلمة المرور.",
		)

	role, label = CREDENTIAL_SOURCES[doctype]
	name_field = {
		"Student": "student_name",
		"Instructor": "instructor_name",
		"Guardian": "guardian_name",
	}[doctype]
	full_name = frappe.db.get_value(doctype, name, name_field) or name

	credentials = create_account(role, name, full_name)
	_link_user(doctype, name, credentials["user"])
	frappe.db.commit()

	return {
		"credentials": credentials,
		"message_ar": f"تم إنشاء حساب {label}: {credentials['username']}",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def reset_account_password(doctype: str = None, name: str = None, persona: str = None):
	"""Issue a fresh password for a person, shown once.

	Takes the person's record rather than a User id so a screen never has to
	know how a teacher's login is linked through Employee.
	"""
	if doctype not in CREDENTIAL_SOURCES or not name:
		return fail(
			message_en="Unsupported record type.",
			message_ar="نوع السجل غير مدعوم.",
		)

	user = _user_of(doctype, name)
	if not user:
		return fail(
			message_en="This person has no account yet.",
			message_ar="لا يوجد حساب لهذا الشخص — أنشئ الحساب أولاً.",
		)

	password = generate_password()
	update_password(user, password)
	# A password handed over on paper must be replaced by one only the person
	# knows, so the first login forces a change unless the school opted out.
	if _force_change_enabled():
		frappe.db.set_value("User", user, "ms_must_change_password", 1, update_modified=False)
	frappe.db.commit()

	row = frappe.db.get_value("User", user, ["username", "full_name"], as_dict=True)
	return {
		"credentials": {
			"user": user,
			"username": row.username or user.split("@")[0],
			"password": password,
			"name": row.full_name,
		},
		"message_ar": "تم إنشاء كلمة مرور جديدة — انسخها الآن، لن تظهر مرة أخرى.",
	}
