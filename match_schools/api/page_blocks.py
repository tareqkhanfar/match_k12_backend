# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""«حجب الصفحات»: closing pages of the portal to a group of people.

A block names who (the whole school, grades, sections, subjects, teachers or
named users — narrowed to students, parents, teachers or staff within them),
what (pages, or the whole portal) and for how long. Three layers apply it:

- `mine` tells the browser and the app which pages are closed and why, so the
  page is replaced by an explanation and the menu shows a lock;
- `check_request` runs inside every endpoint: a call made from a blocked page
  (the web client names its page in `X-MS-Page`) is refused, so a tab left
  open from before the block cannot keep reading that page's data;
- nobody can lock the school out of itself: the principal, Administrator and
  system managers are never blocked, and the home, alerts, profile, password
  and help pages — and this screen — stay open to everyone.

Who a block reaches is worked out per user and cached for two minutes; any
change to a block clears every user's cached answer at once.
"""

import json

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_NURSE,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_SUPERVISOR,
	ROLE_TEACHER,
	ROLE_VICE_PRINCIPAL,
	apply_period,
	fail,
	get_guardian_students,
	get_linked_guardian,
	get_linked_instructor,
	get_linked_student,
	get_role,
	ms_endpoint,
	parse_json_arg,
)

DOCTYPE = "MS Page Block"
EVERYTHING = "*"
# Always open: a blocked person must be able to land somewhere and read why,
# and to change their password. This screen too, so it can never lock itself.
ALWAYS_OPEN = ("/app", "/app/alerts", "/app/profile", "/app/security", "/app/help", "/app/page-blocks")
TARGETS = {
	"school": "المدرسة كاملة",
	"program": "صفوف",
	"student_group": "شعب",
	"course": "مواد",
	"instructor": "معلمون",
	"user": "مستخدمون",
}
AUDIENCES = {"student": "الطلاب", "parent": "أولياء الأمور", "teacher": "المعلمون", "staff": "الموظفون"}
# Modules the portal's frame calls on every page — the header, notifications,
# the session. Refusing them on a blocked page would break the frame around
# the explanation instead of just the page.
SHELL_MODULES = {
	"auth", "security", "notifications", "academic_context", "help_center", "workspace",
	"workspaces", "page_blocks", "alerts", "surveys", "settings", "roles", "moderation",
}
CACHE_SECONDS = 120
VERSION_KEY = "ms_page_blocks_version"


# --- who is never blocked, and who someone is ------------------------------------


def exempt(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user in ("Administrator", "Guest"):
		return True
	if "System Manager" in frappe.get_roles(user):
		return True
	return get_role(user) == ROLE_ADMIN


def audience_of(user: str) -> str | None:
	role = get_role(user)
	if role == ROLE_STUDENT:
		return "student"
	if role == ROLE_PARENT:
		return "parent"
	if role in (ROLE_TEACHER, ROLE_SUPERVISOR):
		return "teacher"
	if role in (ROLE_SECRETARY, ROLE_NURSE, ROLE_VICE_PRINCIPAL):
		return "staff"
	return None


def _group_facts(groups: set[str]) -> tuple[set, set]:
	"""The grades and subjects of these sections."""
	if not groups:
		return set(), set()
	programs = set(
		frappe.get_all("Student Group", filters={"name": ["in", list(groups)]}, pluck="program")
	)
	courses = set(
		frappe.get_all(
			"MS Timetable Slot",
			filters={"student_group": ["in", list(groups)], "active": 1},
			pluck="course",
			distinct=True,
		)
	)
	return {p for p in programs if p}, {c for c in courses if c}


def _student_groups(students: list[str]) -> set[str]:
	if not students:
		return set()
	groups = frappe.get_all(
		"Student Group Student",
		filters={"student": ["in", students], "active": 1, "parenttype": "Student Group"},
		pluck="parent",
	)
	current = set(
		frappe.get_all(
			"Student Group",
			filters=apply_period({"name": ["in", groups or ["-"]], "disabled": 0}, "Student Group"),
			pluck="name",
		)
	)
	return current


def facts_of(user: str) -> dict:
	"""What a block can match this person on: their sections, grades, subjects,
	teacher record."""
	who = audience_of(user)
	groups: set[str] = set()
	courses: set[str] = set()
	instructor = None
	if who == "student":
		groups = _student_groups([s for s in [get_linked_student(user)] if s])
	elif who == "parent":
		groups = _student_groups(get_guardian_students(get_linked_guardian(user)))
	elif who == "teacher":
		instructor = get_linked_instructor(user)
		if instructor:
			rows = frappe.get_all(
				"MS Timetable Slot",
				filters={"instructor": instructor, "active": 1},
				fields=["student_group", "course"],
				limit_page_length=0,
			)
			groups = {r.student_group for r in rows if r.student_group}
			courses = {r.course for r in rows if r.course}
			groups |= set(
				frappe.get_all(
					"Student Group",
					filters=apply_period({"ms_homeroom_instructor": instructor, "disabled": 0}, "Student Group"),
					pluck="name",
				)
			)
	programs, group_courses = _group_facts(groups)
	if who != "teacher":
		courses = group_courses
	return {
		"audience": who,
		"groups": groups,
		"programs": programs,
		"courses": courses,
		"instructor": instructor,
	}


# --- the blocks themselves ----------------------------------------------------------


def _load(doc) -> dict:
	return {
		"id": doc.name,
		"title": doc.title,
		"active": bool(cint(doc.is_active)),
		"targetType": doc.target_type or "school",
		"targets": parse_json_arg(doc.targets, []) or [],
		"audiences": parse_json_arg(doc.audiences, []) or [],
		"everything": bool(cint(doc.block_everything)),
		"pages": parse_json_arg(doc.pages, []) or [],
		"message": doc.message or "",
		"startsOn": str(doc.starts_on or ""),
		"endsOn": str(doc.ends_on or ""),
		"setBy": doc.set_by,
		"setOn": str(doc.set_on or ""),
	}


def _all_blocks() -> list[dict]:
	return [
		_load(d)
		for d in frappe.get_all(
			DOCTYPE,
			fields=[
				"name", "title", "is_active", "target_type", "targets", "audiences",
				"block_everything", "pages", "message", "starts_on", "ends_on", "set_by", "set_on",
			],
			order_by="modified desc",
			limit_page_length=0,
		)
	]


def state_of(block: dict, now=None) -> str:
	now = now or now_datetime()
	if not block["active"]:
		return "off"
	if block["startsOn"] and get_datetime(block["startsOn"]) > now:
		return "scheduled"
	if block["endsOn"] and get_datetime(block["endsOn"]) <= now:
		return "ended"
	return "active"


def reaches(block: dict, facts: dict, user: str) -> bool:
	"""Whether this block applies to this person."""
	kind, targets = block["targetType"], set(block["targets"])
	if kind == "user":
		return user in targets
	if kind == "instructor":
		return bool(facts["instructor"]) and facts["instructor"] in targets
	if facts["audience"] not in set(block["audiences"] or AUDIENCES):
		return False
	if kind == "school":
		return True
	if kind == "program":
		return bool(facts["programs"] & targets)
	if kind == "student_group":
		return bool(facts["groups"] & targets)
	if kind == "course":
		return bool(facts["courses"] & targets)
	return False


def invalidate() -> None:
	frappe.cache.set_value(VERSION_KEY, str(now_datetime()))


def blocks_for(user: str | None = None) -> list[dict]:
	"""The blocks in force for this person now, newest first."""
	user = user or frappe.session.user
	if exempt(user):
		return []
	version = frappe.cache.get_value(VERSION_KEY) or "0"
	key = f"ms_page_blocks:{version}:{user}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached
	now = now_datetime()
	live = [b for b in _all_blocks() if state_of(b, now) == "active"]
	found = []
	if live:
		facts = facts_of(user)
		found = [b for b in live if reaches(b, facts, user)]
	frappe.cache.set_value(key, found, expires_in_sec=CACHE_SECONDS)
	return found


def _covers(page: str, path: str) -> bool:
	return path == page or path.startswith(page.rstrip("/") + "/")


def blocked_by(path: str, user: str | None = None) -> dict | None:
	"""The block closing this page to this person, if any."""
	path = (path or "").split("?")[0].rstrip("/") or "/app"
	if path in ALWAYS_OPEN or not path.startswith("/app"):
		return None
	for b in blocks_for(user):
		if b["everything"] or any(_covers(p, path) for p in b["pages"]):
			return b
	return None


def check_request(module: str):
	"""Refusal for a call made from a blocked page, else None (`ms_endpoint`)."""
	if module in SHELL_MODULES:
		return None
	try:
		page = frappe.get_request_header("X-MS-Page") or ""
	except Exception:
		return None
	if not page:
		return None
	block = blocked_by(page)
	if not block:
		return None
	return fail(
		"This page is blocked.",
		f"هذه الصفحة محجوبة: {block['title']}" + (f" — {block['message']}" if block["message"] else ""),
		data={"blocked": True, "block": block["id"]},
	)


# --- endpoints --------------------------------------------------------------------------

ALL = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)


@frappe.whitelist()
@ms_endpoint(*ALL)
def mine(persona: str = None):
	"""Which pages are closed to the caller, and why."""
	blocks = blocks_for()
	return {
		"everything": any(b["everything"] for b in blocks),
		"pages": sorted({p for b in blocks for p in b["pages"]}),
		"alwaysOpen": list(ALWAYS_OPEN),
		"blocks": [
			{
				"id": b["id"],
				"title": b["title"],
				"message": b["message"],
				"everything": b["everything"],
				"pages": b["pages"],
				"endsOn": b["endsOn"],
			}
			for b in blocks
		],
	}


def _admin(persona):
	if persona != ROLE_ADMIN:
		frappe.throw("حجب الصفحات لإدارة المدرسة فقط.", frappe.PermissionError)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def options(persona: str = None):
	"""What a block can target."""
	_admin(persona)
	groups = frappe.get_all(
		"Student Group",
		filters=apply_period({"disabled": 0}, "Student Group"),
		fields=["name", "student_group_name", "program"],
		order_by="program, student_group_name",
		limit_page_length=0,
	)
	programs = sorted({g.program for g in groups if g.program})
	course_ids = sorted(
		set(frappe.get_all("MS Timetable Slot", filters={"active": 1}, pluck="course", distinct=True))
	)
	return {
		"targets": [{"value": k, "label": v} for k, v in TARGETS.items()],
		"audiences": [{"value": k, "label": v} for k, v in AUDIENCES.items()],
		"programs": [{"value": p, "label": p} for p in programs],
		"groups": [
			{"value": g.name, "label": g.student_group_name or g.name, "hint": g.program or ""}
			for g in groups
		],
		"courses": [{"value": c, "label": c} for c in course_ids],
		"instructors": [
			{"value": i.name, "label": i.instructor_name or i.name}
			for i in frappe.get_all(
				"Instructor",
				filters={"status": "Active"},
				fields=["name", "instructor_name"],
				order_by="instructor_name",
				limit_page_length=0,
			)
		],
		"alwaysOpen": list(ALWAYS_OPEN),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def search_users(q: str = None, persona: str = None):
	"""Accounts to block by name, email or username."""
	_admin(persona)
	q = (q or "").strip()
	if len(q) < 2:
		return {"users": []}
	like = f"%{q}%"
	rows = frappe.db.sql(
		"""select name, full_name, username from tabUser
		where enabled = 1 and user_type in ('System User', 'Website User')
		and name not in ('Administrator', 'Guest')
		and (full_name like %(l)s or name like %(l)s or username like %(l)s)
		order by full_name limit 25""",
		{"l": like},
		as_dict=True,
	)
	from match_schools.api.utils import ROLE_LABELS_AR

	return {
		"users": [
			{
				"value": r.name,
				"label": r.full_name or r.name,
				"hint": " · ".join(x for x in (ROLE_LABELS_AR.get(get_role(r.name) or "", ""), r.username or r.name) if x),
			}
			for r in rows
		]
	}


def _clean(data: dict) -> dict | str:
	title = (data.get("title") or "").strip()
	if not title:
		return "اكتب عنواناً للحجب."
	kind = data.get("targetType") or "school"
	if kind not in TARGETS:
		return "نوع استهداف غير معروف."
	targets = [t for t in (data.get("targets") or []) if t]
	if kind != "school" and not targets:
		return f"اختر {TARGETS[kind]} المحجوب عنهم."
	audiences = [a for a in (data.get("audiences") or []) if a in AUDIENCES]
	if kind not in ("user", "instructor") and not audiences:
		return "اختر فئة واحدة على الأقل (طلاب، أولياء أمور، معلمون، موظفون)."
	everything = bool(data.get("everything"))
	pages = sorted({
		p.rstrip("/") for p in (data.get("pages") or [])
		if isinstance(p, str) and p.startswith("/app/") and p.rstrip("/") not in ALWAYS_OPEN
	})
	if not everything and not pages:
		return "اختر صفحة واحدة على الأقل، أو حجب الموقع بالكامل."
	starts, ends = data.get("startsOn") or None, data.get("endsOn") or None
	if starts and ends and get_datetime(ends) <= get_datetime(starts):
		return "نهاية الحجب يجب أن تكون بعد بدايته."
	return {
		"title": title,
		"is_active": 1 if data.get("active", True) else 0,
		"target_type": kind,
		"targets": json.dumps(targets if kind != "school" else [], ensure_ascii=False),
		"audiences": json.dumps(audiences if kind not in ("user", "instructor") else [], ensure_ascii=False),
		"block_everything": 1 if everything else 0,
		"pages": json.dumps([] if everything else pages, ensure_ascii=False),
		"message": (data.get("message") or "").strip(),
		"starts_on": starts,
		"ends_on": ends,
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_block(payload: str | dict = None, persona: str = None):
	_admin(persona)
	data = parse_json_arg(payload, {}) or {}
	values = _clean(data)
	if isinstance(values, str):
		return fail(values, values)
	doc = frappe.get_doc(DOCTYPE, data["id"]) if data.get("id") else frappe.new_doc(DOCTYPE)
	doc.update(values)
	doc.set_by = frappe.session.user
	doc.set_on = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"block": _with_summary(_load(doc)), "message_ar": "تم حفظ الحجب."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def set_active(block: str = None, active: int = 1, persona: str = None):
	_admin(persona)
	if not block or not frappe.db.exists(DOCTYPE, block):
		return fail("Block not found.", "الحجب غير موجود.")
	doc = frappe.get_doc(DOCTYPE, block)
	doc.is_active = 1 if cint(active) else 0
	doc.set_by = frappe.session.user
	doc.set_on = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"block": _with_summary(_load(doc))}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def delete_block(block: str = None, persona: str = None):
	_admin(persona)
	if block and frappe.db.exists(DOCTYPE, block):
		frappe.delete_doc(DOCTYPE, block, ignore_permissions=True)
		frappe.db.commit()
	return {"deleted": block}


# --- who a block reaches, for the screen that sets it --------------------------------------


def affected(block: dict, limit: int = 40) -> dict:
	"""The accounts a block reaches, by audience."""
	kind, targets = block["targetType"], set(block["targets"])
	audiences = set(block["audiences"] or AUDIENCES)
	users: dict[str, set] = {a: set() for a in AUDIENCES}

	if kind == "user":
		for u in targets:
			who = audience_of(u) or "staff"
			if not exempt(u):
				users.setdefault(who, set()).add(u)
	elif kind == "instructor":
		for i in targets:
			u = frappe.db.get_value("Instructor", i, "ms_user")
			if u and not exempt(u):
				users["teacher"].add(u)
	else:
		groups: set[str] | None
		if kind == "school":
			groups = None
		elif kind == "program":
			groups = set(
				frappe.get_all(
					"Student Group",
					filters=apply_period({"program": ["in", list(targets)], "disabled": 0}, "Student Group"),
					pluck="name",
				)
			)
		elif kind == "student_group":
			groups = targets
		else:
			groups = set(
				frappe.get_all(
					"MS Timetable Slot",
					filters={"course": ["in", list(targets)], "active": 1},
					pluck="student_group",
					distinct=True,
				)
			)
		if "student" in audiences or "parent" in audiences:
			if groups is None:
				students = frappe.get_all("Student", filters={"enabled": 1}, pluck="name", limit_page_length=0)
			else:
				students = frappe.get_all(
					"Student Group Student",
					filters={"parent": ["in", list(groups) or ["-"]], "active": 1, "parenttype": "Student Group"},
					pluck="student",
					limit_page_length=0,
				)
			if "student" in audiences and students:
				users["student"] |= set(
					frappe.get_all("Student", filters={"name": ["in", students], "user": ["is", "set"]}, pluck="user")
				)
			if "parent" in audiences and students:
				guardians = frappe.get_all(
					"Student Guardian",
					filters={"parent": ["in", students], "parenttype": "Student"},
					pluck="guardian",
				)
				if guardians:
					users["parent"] |= set(
						frappe.get_all("Guardian", filters={"name": ["in", guardians], "user": ["is", "set"]}, pluck="user")
					)
		if "teacher" in audiences:
			if groups is None:
				instructors = frappe.get_all("Instructor", filters={"status": "Active"}, pluck="name")
			else:
				filters = {"active": 1}
				if kind == "course":
					filters["course"] = ["in", list(targets)]
				else:
					filters["student_group"] = ["in", list(groups) or ["-"]]
				instructors = set(frappe.get_all("MS Timetable Slot", filters=filters, pluck="instructor", distinct=True))
				if kind != "course" and groups:
					instructors |= set(
						frappe.get_all("Student Group", filters={"name": ["in", list(groups)]}, pluck="ms_homeroom_instructor")
					)
			ids = [i for i in instructors if i]
			if ids:
				users["teacher"] |= set(
					frappe.get_all("Instructor", filters={"name": ["in", ids], "ms_user": ["is", "set"]}, pluck="ms_user")
				)
		if "staff" in audiences and kind == "school":
			for u in frappe.get_all("Has Role", filters={"role": ["in", ["MS Secretary", "MS Nurse", "MS Vice Principal"]], "parenttype": "User"}, pluck="parent"):
				if audience_of(u) == "staff":
					users["staff"].add(u)
		for a in list(users):
			users[a] = {u for u in users[a] if not exempt(u)}

	names = {}
	everyone = [u for a in users.values() for u in a]
	if everyone:
		names = dict(
			frappe.get_all("User", filters={"name": ["in", everyone[:limit]]}, fields=["name", "full_name"], as_list=True)
		)
	return {
		"total": len(set(everyone)),
		"byAudience": {a: len(v) for a, v in users.items() if v},
		"sample": [{"user": u, "name": names.get(u) or u} for u in everyone[:limit]],
	}


def _with_summary(block: dict) -> dict:
	block["state"] = state_of(block)
	labels = {}
	if block["targets"]:
		if block["targetType"] == "student_group":
			labels = dict(frappe.get_all("Student Group", filters={"name": ["in", block["targets"]]}, fields=["name", "student_group_name"], as_list=True))
		elif block["targetType"] == "instructor":
			labels = dict(frappe.get_all("Instructor", filters={"name": ["in", block["targets"]]}, fields=["name", "instructor_name"], as_list=True))
		elif block["targetType"] == "user":
			labels = dict(frappe.get_all("User", filters={"name": ["in", block["targets"]]}, fields=["name", "full_name"], as_list=True))
	block["targetLabels"] = [labels.get(t) or t for t in block["targets"]]
	return block


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def list_blocks(persona: str = None):
	_admin(persona)
	blocks = [_with_summary(b) for b in _all_blocks()]
	for b in blocks:
		b["reach"] = affected(b, limit=0)["total"]
	return {"blocks": blocks}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def preview(payload: str | dict = None, persona: str = None):
	"""Who a block being drawn would reach — before it is saved."""
	_admin(persona)
	data = parse_json_arg(payload, {}) or {}
	block = {
		"targetType": data.get("targetType") or "school",
		"targets": [t for t in (data.get("targets") or []) if t],
		"audiences": [a for a in (data.get("audiences") or []) if a in AUDIENCES],
	}
	if block["targetType"] not in ("user", "instructor") and not block["audiences"]:
		return {"total": 0, "byAudience": {}, "sample": []}
	return affected(block)
