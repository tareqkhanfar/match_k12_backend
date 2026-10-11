"""الأجندة — the school's calendar as events with consequences.

The calendar used to record one thing: whether a day was a holiday. A school's
year holds more — exams, trips, open days, meetings, ceremonies — and each
kind acts on the school differently. An event here has a *type*
(`MS Agenda Type`), and the type carries the effects:

* **closes_school** — a holiday. Synced into the school's Holiday List, which
  is what attendance, lessons, exams and the reports already read, so a
  holiday entered here closes the day everywhere exactly as before.
* **no_lessons** — the audience's lessons are suspended (a trip, an activity
  day). Attendance and the class log stop expecting them, and exam scheduling
  warns.
* **exam_period** — shown on exam scheduling's week as the exam period.
* **staff_only** — the staff's (a meeting); students and families don't see it.
* **notify** — the audience's phones ring when it is added.

An event is for the whole school, some grades, some sections, or the staff.
Types are the school's to add and tune; the ones below are seeded once.
"""

import datetime
import json

import frappe
from frappe.utils import add_days, cint, getdate, nowdate

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	apply_period,
	fail,
	get_default_academic_year,
	instructor_groups,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)

ALL = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
EVENT = "MS Agenda Event"
TYPE = "MS Agenda Type"
CANCELLED = "ملغى"
HOLIDAY_KEY = "holiday"
SEEDED_KEY = "ms_agenda_seeded_v1"
MIGRATED_KEY = "ms_agenda_holidays_migrated_v1"
MAX_DAYS = 120

AUDIENCES = {
	"school": "كل المدرسة",
	"programs": "صفوف محددة",
	"groups": "شعب محددة",
	"staff": "الطاقم فقط",
}

# key, label, colour, icon, effects
DEFAULT_TYPES = [
	("holiday", "عطلة", "red", "calendar", {"closes_school": 1, "notify": 1}),
	("exams", "امتحانات", "violet", "file-check", {"exam_period": 1, "notify": 1}),
	("activity", "نشاط", "green", "star", {}),
	("event", "فعالية / حفل", "pink", "megaphone", {"notify": 1}),
	("trip", "رحلة", "teal", "bus", {"no_lessons": 1, "notify": 1}),
	("meeting", "اجتماع", "blue", "users", {"staff_only": 1, "notify": 1}),
	("parents", "لقاء أولياء الأمور", "amber", "users-2", {"notify": 1}),
	("occasion", "مناسبة", "gray", "flag", {}),
]
EFFECTS = ("closes_school", "no_lessons", "exam_period", "staff_only", "notify")


# --- types ----------------------------------------------------------------------


def seed_types():
	"""The common kinds, once per site; the school edits them afterwards."""
	if frappe.db.get_default(SEEDED_KEY) or not frappe.db.table_exists(TYPE):
		return
	for i, (key, label, color, icon, effects) in enumerate(DEFAULT_TYPES):
		if frappe.db.exists(TYPE, key):
			continue
		doc = frappe.new_doc(TYPE)
		doc.type_key = key
		doc.label = label
		doc.color = color
		doc.icon = icon
		doc.sort_order = i
		doc.is_active = 1
		doc.is_system = 1
		for e in EFFECTS:
			doc.set(e, cint(effects.get(e)))
		doc.insert(ignore_permissions=True)
	frappe.db.set_default(SEEDED_KEY, "1")


@frappe.request_cache
def _types() -> dict:
	rows = frappe.get_all(
		TYPE,
		fields=["name", "label", "color", "icon", "sort_order", "is_active", "is_system", *EFFECTS],
		order_by="sort_order asc, label asc",
		limit_page_length=0,
	)
	return {r.name: r for r in rows}


def _type_row(t) -> dict:
	return {
		"key": t.name,
		"label": t.label,
		"color": t.color or "gray",
		"icon": t.icon or "calendar",
		"sortOrder": cint(t.sort_order),
		"isActive": cint(t.is_active),
		"isSystem": cint(t.is_system),
		**{e: cint(t.get(e)) for e in EFFECTS},
	}


# --- who an event is for ------------------------------------------------------------


def _json(value) -> list:
	return [x for x in (parse_json_arg(value, []) or []) if isinstance(x, str)]


@frappe.request_cache
def _groups_of_programs(programs: tuple) -> set[str]:
	if not programs:
		return set()
	f: dict = {"program": ["in", list(programs)], "disabled": 0}
	apply_period(f, "Student Group")
	return set(frappe.get_all("Student Group", filters=f, pluck="name", limit_page_length=0))


def event_groups(ev) -> set[str] | None:
	"""The sections an event is for; None for the whole school (or the staff)."""
	audience = ev.get("audience") or "school"
	if audience == "groups":
		return set(_json(ev.get("groups")))
	if audience == "programs":
		return _groups_of_programs(tuple(sorted(_json(ev.get("programs")))))
	return None


@frappe.request_cache
def _my_groups(persona: str) -> set[str] | None:
	"""The sections this person belongs to; None = the whole school."""
	if persona in BACK_OFFICE:
		return None
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		return set(instructor_groups(scope.get("instructor"), period=False))
	students = scope.get("students") or []
	return set(
		frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students or [""]], "parenttype": "Student Group", "active": 1},
			pluck="parent",
			limit_page_length=0,
		)
	)


def visible_to(ev, persona: str) -> bool:
	t = _types().get(ev.get("event_type"))
	staff_only = (ev.get("audience") == "staff") or (t and cint(t.staff_only))
	if staff_only and persona not in STAFF:
		return False
	if persona in BACK_OFFICE:
		return True
	groups = event_groups(ev)
	if groups is None:
		return True
	mine = _my_groups(persona) or set()
	return bool(groups & mine)


# --- what a day holds ----------------------------------------------------------------


def _dates(ev) -> list[datetime.date]:
	start = getdate(ev.from_date)
	end = getdate(ev.to_date or ev.from_date)
	if end < start:
		start, end = end, start
	days = min((end - start).days, MAX_DAYS)
	return [start + datetime.timedelta(days=i) for i in range(days + 1)]


def _events_between(date_from, date_to) -> list:
	return frappe.get_all(
		EVENT,
		filters={
			"from_date": ["<=", str(date_to)],
			"status": ["!=", CANCELLED],
		},
		or_filters=[["to_date", ">=", str(date_from)], ["to_date", "is", "not set"]],
		fields=[
			"name", "title", "event_type", "status", "from_date", "to_date", "all_day", "from_time",
			"to_time", "audience", "programs", "groups", "location", "description", "no_lessons",
			"academic_year",
		],
		order_by="from_date asc, from_time asc",
		limit_page_length=0,
	)


def day_events(date, student_group: str | None = None) -> list:
	"""Events on this date for this section (or the whole school)."""
	d = getdate(date)
	out = []
	for ev in _events_between(d, d):
		if getdate(ev.to_date or ev.from_date) < d:
			continue
		groups = event_groups(ev)
		if student_group and groups is not None and student_group not in groups:
			continue
		out.append(ev)
	return out


def suspends_lessons(ev) -> bool:
	t = _types().get(ev.event_type)
	return bool(cint(ev.get("no_lessons")) or (t and (cint(t.no_lessons) or cint(t.closes_school))))


def lessons_suspended(student_group: str, date) -> str | None:
	"""Why this section has no lessons that day (a trip, an activity), or None.
	Holidays are answered by `academic_context.holiday_reason`."""
	for ev in day_events(date, student_group):
		if suspends_lessons(ev):
			return ev.title
	return None


def suspended_days(date_from, date_to) -> tuple[set[str], dict[str, set[str]]]:
	"""Days whose lessons the agenda suspends between two dates: for the whole
	school, and per section. For reports that count what should have happened."""
	school: set[str] = set()
	by_group: dict[str, set[str]] = {}
	for ev in _events_between(getdate(date_from), getdate(date_to)):
		if not suspends_lessons(ev):
			continue
		days = {str(d) for d in _dates(ev)}
		groups = event_groups(ev)
		if groups is None:
			school |= days
		else:
			for g in groups:
				by_group.setdefault(g, set()).update(days)
	return school, by_group


def exam_period(date, student_group: str | None = None) -> str | None:
	for ev in day_events(date, student_group):
		t = _types().get(ev.event_type)
		if t and cint(t.exam_period):
			return ev.title
	return None


# --- the holiday list ------------------------------------------------------------------


def _sync_holidays(doc, old_title: str | None = None, old_dates: list[str] | None = None, remove: bool = False):
	"""Keep the Holiday List saying what a holiday event says.

	Only days this event added are ever removed, and only while they still
	carry its title — a day the school entered as a holiday some other way
	stays a holiday."""
	from match_schools.api.academic_context import _holiday_list_name

	name = _holiday_list_name()
	if not name:
		return []
	hl = frappe.get_doc("Holiday List", name)
	before = len(hl.holidays)
	stale = set(old_dates or [])
	if stale:
		hl.holidays = [
			r
			for r in hl.holidays
			if not (
				str(getdate(r.holiday_date)) in stale
				and not cint(r.weekly_off)
				and (r.description or "") in (old_title or "", doc.title)
			)
		]
	added: list[str] = []
	t = _types().get(doc.event_type)
	closes = (
		not remove
		and t
		and cint(t.closes_school)
		and doc.status != CANCELLED
		and (doc.audience or "school") in ("school", "staff")
	)
	if closes:
		taken = {str(getdate(r.holiday_date)) for r in hl.holidays}
		lo = getdate(hl.from_date) if hl.from_date else None
		hi = getdate(hl.to_date) if hl.to_date else None
		for d in _dates(doc):
			key = str(d)
			if key in taken or (lo and d < lo) or (hi and d > hi):
				continue
			hl.append("holidays", {"holiday_date": d, "description": doc.title})
			added.append(key)
	if added or len(hl.holidays) != before:
		hl.save(ignore_permissions=True)
	return added


def migrate_holidays():
	"""Once per site: the holidays already on the list become agenda events —
	consecutive days with the same name as one event — so the agenda starts
	with what the calendar showed. The list rows stay; the events own them."""
	if frappe.db.get_default(MIGRATED_KEY) or not frappe.db.table_exists(EVENT):
		return
	from match_schools.api.academic_context import _holiday_list_name

	name = _holiday_list_name()
	if not name or not frappe.db.exists(TYPE, HOLIDAY_KEY):
		return
	rows = frappe.get_all(
		"Holiday",
		filters={"parent": name, "parenttype": "Holiday List", "weekly_off": 0},
		fields=["holiday_date", "description"],
		order_by="holiday_date asc",
		limit_page_length=0,
	)
	runs: list[dict] = []
	for r in rows:
		d = getdate(r.holiday_date)
		title = (frappe.utils.strip_html(r.description or "") or "عطلة").strip()[:140]
		last = runs[-1] if runs else None
		if last and last["title"] == title and (d - last["to"]).days == 1:
			last["to"] = d
			last["dates"].append(str(d))
		else:
			runs.append({"title": title, "from": d, "to": d, "dates": [str(d)], "raw": r.description or ""})
	for run in runs:
		doc = frappe.new_doc(EVENT)
		doc.title = run["title"]
		doc.event_type = HOLIDAY_KEY
		doc.status = "مؤكد"
		doc.from_date = run["from"]
		doc.to_date = run["to"]
		doc.all_day = 1
		doc.audience = "school"
		doc.synced_dates = json.dumps(run["dates"])
		doc.notified = 1
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		if run["raw"] != run["title"]:
			# The list rows keep matching the event, so removing it removes them.
			frappe.db.sql(
				"""UPDATE `tabHoliday` SET description = %s
				    WHERE parent = %s AND parenttype = 'Holiday List' AND weekly_off = 0
				      AND holiday_date IN %s""",
				(run["title"], name, tuple(run["dates"])),
			)
	frappe.db.set_default(MIGRATED_KEY, "1")


# --- reading -----------------------------------------------------------------------------


def _row(ev, types: dict) -> dict:
	t = types.get(ev.event_type)
	groups = _json(ev.get("groups"))
	programs = _json(ev.get("programs"))
	return {
		"id": ev.name,
		"source": "event",
		"title": ev.title,
		"type": ev.event_type,
		"typeLabel": t.label if t else ev.event_type,
		"color": (t.color if t else "") or "gray",
		"icon": (t.icon if t else "") or "calendar",
		"status": ev.get("status") or "مؤكد",
		"from": str(ev.from_date),
		"to": str(ev.to_date or ev.from_date),
		"allDay": cint(ev.get("all_day")),
		"fromTime": str(ev.get("from_time") or "")[:5],
		"toTime": str(ev.get("to_time") or "")[:5],
		"audience": ev.get("audience") or "school",
		"audienceLabel": AUDIENCES.get(ev.get("audience") or "school", ""),
		"groups": groups,
		"programs": programs,
		"location": ev.get("location") or "",
		"description": ev.get("description") or "",
		"noLessons": 1 if suspends_lessons(ev) else 0,
		"closesSchool": cint(t.closes_school) if t else 0,
		"examPeriod": cint(t.exam_period) if t else 0,
		"staffOnly": 1 if (ev.get("audience") == "staff" or (t and cint(t.staff_only))) else 0,
	}


@frappe.whitelist()
@ms_endpoint(*ALL)
def options(persona: str = None):
	"""The types, and — for the office — the grades and sections an event can name."""
	types = [_type_row(t) for t in _types().values()]
	out = {
		"types": types,
		"audiences": [{"key": k, "label": v} for k, v in AUDIENCES.items()],
		"canEdit": persona in BACK_OFFICE,
		"programs": [],
		"groups": [],
	}
	if persona in BACK_OFFICE:
		f: dict = {"disabled": 0}
		apply_period(f, "Student Group")
		out["programs"] = [
			{"id": p.name, "label": p.program_name or p.name}
			for p in frappe.get_all("Program", fields=["name", "program_name"], order_by="name asc")
		]
		out["groups"] = [
			{"id": g.name, "label": g.student_group_name or g.name, "program": g.program or ""}
			for g in frappe.get_all(
				"Student Group",
				filters=f,
				fields=["name", "student_group_name", "program"],
				order_by="program asc, student_group_name asc",
				limit_page_length=0,
			)
		]
	return out


@frappe.whitelist()
@ms_endpoint(*ALL)
def events(date_from: str = None, date_to: str = None, event_type: str = None, persona: str = None):
	"""What this person's calendar shows between two dates: the events they
	may see, the weekly days off, and holidays entered outside the agenda."""
	start = getdate(date_from or add_days(nowdate(), -45))
	end = getdate(date_to or add_days(nowdate(), 120))
	if (end - start).days > 400:
		end = start + datetime.timedelta(days=400)
	types = _types()
	rows = []
	owned: set[str] = set()
	for ev in _events_between(start, end):
		if event_type and ev.event_type != event_type:
			continue
		if not visible_to(ev, persona):
			continue
		rows.append(_row(ev, types))
		if ev.event_type and types.get(ev.event_type) and cint(types[ev.event_type].closes_school):
			owned |= {str(d) for d in _dates(ev)}

	from match_schools.api.academic_context import _holiday_list_name

	name = _holiday_list_name()
	if name and (not event_type or event_type == HOLIDAY_KEY):
		for h in frappe.get_all(
			"Holiday",
			filters={
				"parent": name,
				"parenttype": "Holiday List",
				"holiday_date": ["between", [str(start), str(end)]],
			},
			fields=["name", "holiday_date", "description", "weekly_off"],
			limit_page_length=0,
		):
			key = str(getdate(h.holiday_date))
			if key in owned:
				continue
			weekly = cint(h.weekly_off)
			rows.append(
				{
					"id": h.name,
					"source": "weekly" if weekly else "holiday",
					"title": frappe.utils.strip_html(h.description or "") or ("عطلة أسبوعية" if weekly else "عطلة"),
					"type": HOLIDAY_KEY,
					"typeLabel": "عطلة أسبوعية" if weekly else "عطلة",
					"color": "gray" if weekly else "red",
					"icon": "calendar",
					"status": "مؤكد",
					"from": key,
					"to": key,
					"allDay": 1,
					"fromTime": "",
					"toTime": "",
					"audience": "school",
					"audienceLabel": AUDIENCES["school"],
					"groups": [],
					"programs": [],
					"location": "",
					"description": "",
					"noLessons": 1,
					"closesSchool": 1,
					"examPeriod": 0,
					"staffOnly": 0,
				}
			)
	rows.sort(key=lambda r: (r["from"], r["fromTime"]))
	return {"events": rows, "from": str(start), "to": str(end), "canEdit": persona in BACK_OFFICE}


@frappe.whitelist()
@ms_endpoint(*ALL)
def upcoming(days: int = 14, persona: str = None):
	"""The next few weeks, for a dashboard card."""
	start = getdate(nowdate())
	end = start + datetime.timedelta(days=max(1, min(cint(days) or 14, 120)))
	types = _types()
	out = []
	for ev in _events_between(start, end):
		if visible_to(ev, persona):
			row = _row(ev, types)
			row["inDays"] = max(0, (getdate(ev.from_date) - start).days)
			out.append(row)
	return {"events": out[:30]}


@frappe.whitelist()
@ms_endpoint(*ALL)
def day(date: str = None, student_group: str = None, persona: str = None):
	"""One day for one section: closed, lessons suspended, exam period, and the events."""
	from match_schools.api.academic_context import holiday_reason

	d = getdate(date or nowdate())
	types = _types()
	evs = [e for e in day_events(d, student_group) if visible_to(e, persona)]
	return {
		"date": str(d),
		"closed": holiday_reason(d) or "",
		"noLessons": next((e.title for e in evs if suspends_lessons(e)), ""),
		"examPeriod": next((e.title for e in evs if types.get(e.event_type) and cint(types[e.event_type].exam_period)), ""),
		"events": [_row(e, types) for e in evs],
	}


# --- writing -------------------------------------------------------------------------------


def _audience_users(doc) -> list[str]:
	"""Who an event's notice reaches: the section's pupils and guardians, or
	the whole school's; the teachers always; nobody else for a staff event."""
	from match_schools.api.communication import _guardian_users, _instructor_users

	t = _types().get(doc.event_type)
	staff_only = doc.audience == "staff" or (t and cint(t.staff_only))
	users = list(_instructor_users())
	if not staff_only:
		groups = event_groups(doc)
		if groups is None:
			students = frappe.get_all("Student", filters={"enabled": 1}, pluck="name", limit_page_length=0)
		else:
			students = frappe.get_all(
				"Student Group Student",
				filters={"parent": ["in", list(groups) or [""]], "parenttype": "Student Group", "active": 1},
				pluck="student",
				limit_page_length=0,
			)
		users += frappe.get_all(
			"Student", filters={"name": ["in", students or [""]], "user": ["is", "set"]}, pluck="user",
			limit_page_length=0,
		)
		users += _guardian_users(students)
	return sorted({u for u in users if u})


def _notify(doc):
	t = _types().get(doc.event_type)
	if not t or not cint(t.notify) or cint(doc.notified) or doc.status == CANCELLED:
		return
	from match_schools import push

	when = str(doc.from_date) if not doc.to_date or doc.to_date == doc.from_date else f"{doc.from_date} — {doc.to_date}"
	push.notify(
		_audience_users(doc),
		f"{t.label}: {doc.title}",
		when + (f" · {doc.location}" if doc.location else ""),
		channel="announcements",
		type="agenda",
		id=doc.name,
	)
	frappe.db.set_value(EVENT, doc.name, "notified", 1, update_modified=False)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_event(payload: str | dict, persona: str = None):
	data = parse_json_arg(payload) or {}
	title = str(data.get("title") or "").strip()
	if not title:
		return fail("A title is required.", "اكتب عنوان الحدث.")
	if data.get("type") not in _types():
		return fail("Choose a type.", "اختر نوع الحدث.")
	if not data.get("from"):
		return fail("A date is required.", "حدّد تاريخ الحدث.")
	start, end = getdate(data["from"]), getdate(data.get("to") or data["from"])
	if end < start:
		return fail("The end is before the start.", "تاريخ النهاية قبل تاريخ البداية.")
	if (end - start).days > MAX_DAYS:
		return fail("Too long.", f"الحدث الواحد لا يتجاوز {MAX_DAYS} يوماً.")
	audience = data.get("audience") if data.get("audience") in AUDIENCES else "school"
	groups = [g for g in (data.get("groups") or []) if frappe.db.exists("Student Group", g)]
	programs = [p for p in (data.get("programs") or []) if frappe.db.exists("Program", p)]
	if audience == "groups" and not groups:
		return fail("Choose the sections.", "اختر الشعب المعنية.")
	if audience == "programs" and not programs:
		return fail("Choose the grades.", "اختر الصفوف المعنية.")
	all_day = 1 if cint(data.get("allDay", 1)) else 0
	if not all_day and not (data.get("fromTime") and data.get("toTime")):
		return fail("Times are required.", "حدّد وقت البداية والنهاية، أو اجعله طوال اليوم.")

	existing = data.get("id") and frappe.db.exists(EVENT, data["id"])
	doc = frappe.get_doc(EVENT, data["id"]) if existing else frappe.new_doc(EVENT)
	old_title, old_dates = (doc.title, _json(doc.synced_dates)) if existing else (None, [])
	doc.title = title[:140]
	doc.event_type = data["type"]
	doc.status = data.get("status") if data.get("status") in ("مؤكد", "مبدئي", CANCELLED) else "مؤكد"
	doc.from_date = start
	doc.to_date = end
	doc.all_day = all_day
	doc.from_time = None if all_day else data.get("fromTime")
	doc.to_time = None if all_day else data.get("toTime")
	doc.audience = audience
	doc.groups = json.dumps(groups if audience == "groups" else [])
	doc.programs = json.dumps(programs if audience == "programs" else [])
	doc.location = str(data.get("location") or "")[:140]
	doc.description = str(data.get("description") or "")[:2000]
	doc.no_lessons = 1 if cint(data.get("noLessons")) else 0
	doc.academic_year = doc.academic_year or get_default_academic_year()
	doc.save(ignore_permissions=True)

	# The days it added before are taken off and the current ones put on, so
	# what it owns now is exactly what this save added.
	added = _sync_holidays(doc, old_title, old_dates)
	doc.db_set("synced_dates", json.dumps(sorted(added)), update_modified=False)
	if cint(data.get("notify", 1)):
		_notify(doc)
	frappe.db.commit()
	return _row(frappe.get_doc(EVENT, doc.name), _types())


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_event(event: str, persona: str = None):
	"""The event goes, and the holiday days it added with it."""
	if not frappe.db.exists(EVENT, event):
		return fail("Not found.", "الحدث غير موجود.")
	doc = frappe.get_doc(EVENT, event)
	_sync_holidays(doc, doc.title, _json(doc.synced_dates), remove=True)
	frappe.delete_doc(EVENT, event, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": event}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_type(payload: str | dict, persona: str = None):
	"""A kind of event and its effects — the school's to add and tune."""
	data = parse_json_arg(payload) or {}
	label = str(data.get("label") or "").strip()
	if not label:
		return fail("A name is required.", "اكتب اسم النوع.")
	key = str(data.get("key") or "").strip()
	if key and frappe.db.exists(TYPE, key):
		doc = frappe.get_doc(TYPE, key)
	else:
		doc = frappe.new_doc(TYPE)
		doc.type_key = frappe.scrub(key) if key else f"t{frappe.generate_hash(length=6)}"
		doc.is_system = 0
	doc.label = label[:60]
	doc.color = str(data.get("color") or "gray")[:20]
	doc.icon = str(data.get("icon") or "calendar")[:40]
	doc.sort_order = cint(data.get("sortOrder"))
	doc.is_active = 1 if cint(data.get("isActive", 1)) else 0
	for e in EFFECTS:
		if e in data:
			doc.set(e, 1 if cint(data.get(e)) else 0)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _type_row(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_type(key: str, persona: str = None):
	if not frappe.db.exists(TYPE, key):
		return fail("Not found.", "النوع غير موجود.")
	if frappe.db.exists(EVENT, {"event_type": key}):
		return fail("In use.", "هذا النوع مستخدم في أحداث — أوقفه بدل حذفه، أو انقل أحداثه لنوع آخر.")
	frappe.delete_doc(TYPE, key, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": key}
