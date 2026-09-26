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


def free_username(username: str) -> str:
	"""The username itself, or the next number up when it is taken.

	Records named by a person's name rather than a numbered series (a school
	that names teachers «أ. رانيا سمير») carry no digits, so every one of them
	would build the same `t1260001` and two people would share a login name.
	"""
	def taken(u: str) -> bool:
		return bool(
			frappe.db.exists("User", {"username": u}) or frappe.db.exists("User", f"{u}@{login_domain()}")
		)

	if not taken(username):
		return username
	m = re.match(r"^(\D+\d{3})(\d+)$", username)
	if not m:
		return username
	head, number = m[1], m[2]
	for n in range(int(number) + 1, int(number) + 10000):
		candidate = f"{head}{str(n).zfill(len(number))}"
		if not taken(candidate):
			return candidate
	return username


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
	username = free_username(build_username(persona, docname, year))
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
	"MS Staff Member": (ROLE_SECRETARY, "السكرتير"),
}

# Where each record keeps the person's name.
NAME_FIELD = {
	"Student": "student_name",
	"Instructor": "instructor_name",
	"Guardian": "guardian_name",
	"MS Staff Member": "full_name",
}


def _assert_may_manage(persona: str, doctype: str):
	"""Office staff accounts are the principal's to manage — a secretary does
	not reset a colleague's password."""
	if doctype == "MS Staff Member" and persona != ROLE_ADMIN:
		frappe.throw(_("حسابات السكرتارية يديرها مدير المدرسة فقط."), frappe.PermissionError)


def _user_of(doctype: str, name: str) -> str | None:
	"""The User linked to a person's record.

	Student, Guardian and staff hold the link directly. An Instructor holds it
	in `ms_user` — the same field the login reads first (`get_linked_instructor`)
	— then through Employee. The name is matched only as a last resort, and
	only to a teacher account nobody else claims: a parent who happens to share
	a teacher's name must never be handed that teacher's new password.
	"""
	if doctype in ("Student", "Guardian", "MS Staff Member"):
		return frappe.db.get_value(doctype, name, "user")

	if doctype == "Instructor":
		direct = frappe.db.get_value("Instructor", name, "ms_user")
		if direct and frappe.db.exists("User", direct):
			return direct
		employee = frappe.db.get_value("Instructor", name, "employee")
		if employee:
			user = frappe.db.get_value("Employee", employee, "user_id")
			if user:
				return user
		return _unclaimed_teacher_by_name(name)
	return None


def _unclaimed_teacher_by_name(instructor: str) -> str | None:
	"""The one teacher account carrying this instructor's name, if exactly one
	does and no other instructor is linked to it."""
	full_name = frappe.db.get_value("Instructor", instructor, "instructor_name")
	if not full_name:
		return None
	users = frappe.get_all("User", filters={"full_name": full_name, "enabled": 1}, pluck="name")
	teachers = [
		u for u in users
		if FRAPPE_ROLE_BY_PERSONA[ROLE_TEACHER] in frappe.get_roles(u)
		and not frappe.db.exists("Instructor", {"ms_user": u, "name": ["!=", instructor]})
	]
	return teachers[0] if len(teachers) == 1 else None


def _link_user(doctype: str, name: str, user: str):
	"""Record the User on the person's own record — explicitly, so the login
	finds its person without guessing by name."""
	if doctype in ("Student", "Guardian", "MS Staff Member"):
		frappe.db.set_value(doctype, name, "user", user, update_modified=False)
	elif doctype == "Instructor":
		frappe.db.set_value("Instructor", name, "ms_user", user, update_modified=False)
		employee = frappe.db.get_value("Instructor", name, "employee")
		if employee and not frappe.db.get_value("Employee", employee, "user_id"):
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
	_assert_may_manage(persona, doctype)
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
	_assert_may_manage(persona, doctype)
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
	name_field = NAME_FIELD[doctype]
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
	_assert_may_manage(persona, doctype)

	user = _user_of(doctype, name)
	if not user:
		return fail(
			message_en="This person has no account yet.",
			message_ar="لا يوجد حساب لهذا الشخص — أنشئ الحساب أولاً.",
		)
	if _protected_user(user):
		return fail("Administrator account.", "هذا حساب مدير النظام — لا تُغيَّر كلمة مروره من هنا.")
	if doctype == "Instructor":
		_link_user(doctype, name, user)

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


def _protected_user(user: str) -> bool:
	"""Accounts a bulk reset must never touch: the site's own administrators.

	A teacher record can end up linked to one (a principal who also teaches,
	or the name fallback matching the wrong User), and a new password there
	would lock the school out of its own system.
	"""
	if user == "Administrator":
		return True
	return "System Manager" in frappe.get_roles(user)


# How each kind of person is named in the bulk tool and on its sheet.
BULK_NOUN = {
	"Instructor": ("المعلم", "المعلمين"),
	"Student": ("الطالب", "الطلاب"),
	"Guardian": ("ولي الأمر", "أولياء الأمور"),
	"MS Staff Member": ("السكرتير", "السكرتارية"),
}


def _group_labels(doctype: str, names: list[str]) -> dict[str, str]:
	"""A second column for the sheet: a student's section, a parent's children.

	Slips are handed out class by class, so the sheet has to say where each
	one goes.
	"""
	if not names:
		return {}
	if doctype == "Student":
		rows = frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", names], "active": 1, "parenttype": "Student Group"},
			fields=["student", "parent"],
			limit_page_length=0,
		)
		labels = {
			g.name: g.student_group_name or g.name
			for g in frappe.get_all(
				"Student Group",
				filters={"name": ["in", list({r.parent for r in rows}) or [""]], "disabled": 0},
				fields=["name", "student_group_name"],
			)
		}
		out: dict[str, str] = {}
		for r in rows:
			if r.parent in labels and r.student not in out:
				out[r.student] = labels[r.parent]
		return out
	if doctype == "Guardian":
		rows = frappe.get_all(
			"Student Guardian",
			filters={"guardian": ["in", names], "parenttype": "Student"},
			fields=["guardian", "parent"],
			limit_page_length=0,
		)
		kids = {
			s.name: s.student_name
			for s in frappe.get_all(
				"Student", filters={"name": ["in", list({r.parent for r in rows}) or [""]]},
				fields=["name", "student_name"],
			)
		}
		out = {}
		for r in rows:
			if r.parent in kids:
				out.setdefault(r.guardian, []).append(kids[r.parent])
		return {g: "، ".join(v) for g, v in out.items()}
	return {}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def account_candidates(doctype: str = None, group: str = None, persona: str = None):
	"""Everyone of one kind the bulk tool may issue to, with whether they
	already have a login — narrowed to one section for students and parents."""
	if doctype not in BULK_NOUN:
		return fail("Unsupported record type.", "نوع السجل غير مدعوم.")
	_assert_may_manage(persona, doctype)

	names: list[str] | None = None
	if group and doctype in ("Student", "Guardian"):
		students = frappe.get_all(
			"Student Group Student",
			filters={"parent": group, "parenttype": "Student Group", "active": 1},
			pluck="student",
			limit_page_length=0,
		)
		if doctype == "Student":
			names = students
		else:
			names = list(
				dict.fromkeys(
					frappe.get_all(
						"Student Guardian",
						filters={"parent": ["in", students or [""]], "parenttype": "Student"},
						pluck="guardian",
						limit_page_length=0,
					)
				)
			)

	filters: dict = {}
	if names is not None:
		filters["name"] = ["in", names or [""]]
	if doctype == "Student":
		filters["enabled"] = 1
	elif doctype == "Instructor":
		filters["status"] = "Active"
	elif doctype == "MS Staff Member":
		_sync_staff()
	field = NAME_FIELD[doctype]
	rows = frappe.get_all(doctype, filters=filters, fields=["name", field], order_by=f"{field} asc", limit_page_length=3000)
	labels = _group_labels(doctype, [r.name for r in rows])

	out = []
	for r in rows:
		user = _user_of(doctype, r.name)
		out.append(
			{
				"id": r.name,
				"name": r.get(field) or r.name,
				"hint": labels.get(r.name, ""),
				"hasAccount": bool(user),
			}
		)

	groups = []
	if doctype in ("Student", "Guardian"):
		from match_schools.api.utils import apply_period

		groups = [
			{"id": g.name, "label": g.student_group_name or g.name}
			for g in frappe.get_all(
				"Student Group",
				filters=apply_period({"disabled": 0}, "Student Group"),
				fields=["name", "student_group_name"],
				order_by="student_group_name",
				limit_page_length=500,
			)
		]
	singular, plural = BULK_NOUN[doctype]
	return {"people": out, "groups": groups, "noun": singular, "nounPlural": plural}


def _sync_staff():
	"""A file for every secretary account that has none yet.

	Secretaries used to be bare Users; each gets their file the first time
	the office looks, so nobody is missing from the list.
	"""
	role = FRAPPE_ROLE_BY_PERSONA[ROLE_SECRETARY]
	users = frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
	have = set(frappe.get_all("MS Staff Member", filters={"user": ["is", "set"]}, pluck="user"))
	for u in users:
		if u in have or u in ("Administrator", "Guest") or not frappe.db.exists("User", u):
			continue
		if "System Manager" in frappe.get_roles(u):
			continue
		info = frappe.db.get_value("User", u, ["full_name", "mobile_no", "enabled", "email", "creation"], as_dict=True)
		doc = frappe.new_doc("MS Staff Member")
		doc.full_name = info.full_name or u.split("@")[0]
		doc.user = u
		doc.phone = info.mobile_no
		doc.status = "Active" if cint(info.enabled) else "Inactive"
		if info.email and not info.email.endswith("@" + login_domain()):
			doc.email = info.email
		doc.joining_date = str(info.creation)[:10]
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def issue_accounts(doctype: str = None, names: str | list = None, mode: str = "all", persona: str = None):
	"""Logins for the chosen people of one kind, returned once as an Excel sheet.

	`mode` "all" creates an account for whoever has none and issues a new
	password to whoever has one; "missing" only creates, leaving working
	logins alone. The sheet is built here rather than on the screen because
	this is the only moment the passwords are readable.
	"""
	from match_schools.api.utils import parse_json_arg

	if doctype not in BULK_NOUN:
		return fail("Unsupported record type.", "نوع السجل غير مدعوم.")
	_assert_may_manage(persona, doctype)
	singular, plural = BULK_NOUN[doctype]
	role = CREDENTIAL_SOURCES[doctype][0]
	field = NAME_FIELD[doctype]

	names = list(dict.fromkeys(parse_json_arg(names) or []))
	if not names:
		return fail("Choose at least one person.", "اختر شخصاً واحداً على الأقل.")
	if len(names) > 1500:
		return fail("Too many at once.", "الحد الأقصى 1500 في المرة الواحدة.")
	mode = "missing" if mode == "missing" else "all"
	labels = _group_labels(doctype, names)

	rows = []
	for name in names:
		if not frappe.db.exists(doctype, name):
			continue
		full_name = frappe.db.get_value(doctype, name, field) or name
		row = {
			"teacher": name,
			"name": full_name,
			"hint": labels.get(name, ""),
			"user": "",
			"username": "",
			"password": "",
		}
		user = _user_of(doctype, name)
		if user and mode == "missing":
			# Listed with their existing login, so the sheet covers the whole class.
			row.update(
				user=user,
				username=frappe.db.get_value("User", user, "username") or user.split("@")[0],
				status="skipped",
				note="لديه حساب — كلمة المرور لم تتغير",
			)
		elif user and _protected_user(user):
			row.update(user=user, status="skipped", note="حساب مدير النظام — لم يُغيَّر")
		elif user:
			if doctype == "Instructor":
				# Found by Employee or by name: make the link explicit now.
				_link_user(doctype, name, user)
			password = generate_password()
			update_password(user, password)
			if _force_change_enabled():
				frappe.db.set_value("User", user, "ms_must_change_password", 1, update_modified=False)
			u = frappe.db.get_value("User", user, ["username", "enabled"], as_dict=True)
			row.update(
				user=user,
				username=u.username or user.split("@")[0],
				password=password,
				status="reset",
				note="" if cint(u.enabled) else "الحساب معطّل — فعّله ليتمكن من الدخول",
			)
		else:
			creds = create_account(role, name, full_name)
			_link_user(doctype, name, creds["user"])
			row.update(user=creds["user"], username=creds["username"], password=creds["password"], status="created", note="")
		rows.append(row)
	frappe.db.commit()

	created = sum(r["status"] == "created" for r in rows)
	reset = sum(r["status"] == "reset" for r in rows)
	hint_label = {"Student": "الشعبة", "Guardian": "الأبناء"}.get(doctype)
	return {
		"rows": rows,
		"created": created,
		"reset": reset,
		"skipped": len(rows) - created - reset,
		"file": _accounts_sheet(rows, singular, f"حسابات {plural}", hint_label),
		"filename": f"حسابات {plural}-{frappe.utils.today()}.xlsx",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def issue_teacher_accounts(names: str | list = None, mode: str = "all", persona: str = None):
	"""The teachers' form of `issue_accounts`, kept for screens already calling it."""
	return issue_accounts.__wrapped__(doctype="Instructor", names=names, mode=mode, persona=persona)


def _accounts_sheet(rows: list[dict], noun: str = "المعلم", title: str = "حسابات المعلمين", hint_label: str | None = None) -> str:
	"""The issued logins as a base64 .xlsx, one person per row."""
	import base64
	import io

	from openpyxl import Workbook
	from openpyxl.styles import Alignment, Font, PatternFill

	status = {"created": "حساب جديد", "reset": "كلمة مرور جديدة", "skipped": "لم يُغيَّر"}
	header = ["#", noun] + ([hint_label] if hint_label else []) + [
		"اسم المستخدم", "المستخدم (البريد)", "كلمة المرور الجديدة", "الحالة", "ملاحظة"
	]
	wb = Workbook()
	ws = wb.active
	ws.title = title[:31]
	ws.sheet_view.rightToLeft = True
	ws.append(header)
	for cell in ws[1]:
		cell.font = Font(bold=True, color="FFFFFF")
		cell.fill = PatternFill("solid", fgColor="0550AE")
		cell.alignment = Alignment(horizontal="center", vertical="center")
	for i, r in enumerate(rows, 1):
		ws.append(
			[i, r["name"]] + ([r.get("hint") or ""] if hint_label else [])
			+ [r["username"], r["user"], r["password"], status[r["status"]], r.get("note") or ""]
		)
	from openpyxl.utils import get_column_letter

	widths = [5, 30] + ([30] if hint_label else []) + [16, 32, 20, 16, 36]
	for idx, width in enumerate(widths, 1):
		ws.column_dimensions[get_column_letter(idx)].width = width
	# Passwords are typed from this sheet: a monospaced face tells 8 from B.
	first = 4 if hint_label else 3
	for row in ws.iter_rows(min_row=2, min_col=first, max_col=first + 2):
		for cell in row:
			cell.font = Font(name="Consolas", bold=cell.column == first + 2)
	ws.freeze_panes = "A2"
	buffer = io.BytesIO()
	wb.save(buffer)
	return base64.b64encode(buffer.getvalue()).decode()
