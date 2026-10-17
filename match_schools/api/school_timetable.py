# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The whole school's week on one sheet: «بناء الجدول الدراسي للمدرسة».

The class builder edits one section and the teacher builder one teacher; this
screen edits every lesson at once, the way schools draw it on paper — a row
per teacher, a column per day and period, the section in the cell. It writes
the same `MS Timetable Slot` rows the other two do, so all three stay views of
one week.

The checks run twice on purpose. The page runs them on every keystroke so a
clash lights up the moment it is made; `validate` here runs the same rules on
save, because a week that double-books a teacher must not reach the register
whatever the browser said.

Generating a whole school's term is thousands of lessons — far past what one
request survives — so it runs as a background job and the page polls it.
"""

import re

import frappe
from frappe.utils import cint, get_datetime, getdate, now_datetime, today

from match_schools.api import scheduling as sched
from match_schools.api import timetable_grid as tg
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	apply_period,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
	sync_group_instructors,
)

JOB_KEY = "ms_school_timetable_job"
GRADE_NUMBERS = {
	"الأول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5, "السادس": 6,
	"السابع": 7, "الثامن": 8, "التاسع": 9, "العاشر": 10, "الحادي عشر": 11, "الثاني عشر": 12,
}
KINDERGARTEN = ("التمهيدي", "البستان", "الروضة", "الحضانة")
# Short names for the cell, where «التربية التكنولوجية والبرمجة» has 56px.
SUBJECT_SHORT = {
	"اللغة العربية": "عربي", "الرياضيات": "رياضيات", "اللغة الإنجليزية": "إنجليزي",
	"التربية الدينية": "دين", "التربية الإسلامية": "دين", "القرآن الكريم": "قرآن",
	"العلوم": "علوم", "العلوم العامة": "علوم", "الفيزياء": "فيزياء", "الكيمياء": "كيمياء",
	"الأحياء": "أحياء", "التربية البدنية": "رياضة", "الفنون والحرف": "فنون",
	"التربية الفنية": "فنون", "التربية التكنولوجية والبرمجة": "تكنولوجيا",
	"التكنولوجيا": "تكنولوجيا", "الجغرافيا": "جغرافيا", "التاريخ": "تاريخ",
	"الخط": "خط", "التربية الاجتماعية": "اجتماعيات", "الدراسات الاجتماعية": "اجتماعيات",
	"التربية الوطنية": "وطنية", "اللغة العبرية": "عبري", "اللغة الفرنسية": "فرنسي",
	"الموسيقى": "موسيقى", "ارشاد": "إرشاد", "الإرشاد": "إرشاد",
}


# --- labels -------------------------------------------------------------------


def _grade_of(program: str | None, label: str) -> tuple[int, str]:
	"""(sort order, short grade) for a section: «الصف الثاني عشر» → (12, "12")."""
	text = (program or label or "").replace("الصف", "").strip()
	text = re.sub(r"\s*-\s*.*$", "", text).strip()
	if text in GRADE_NUMBERS:
		n = GRADE_NUMBERS[text]
		return n, str(n)
	for i, kg in enumerate(KINDERGARTEN):
		if kg in text:
			return -len(KINDERGARTEN) + i, "ت"
	digits = re.search(r"\d+", text)
	if digits:
		return int(digits.group()), digits.group()
	return 99, text[:4]


def _short_group(label: str, program: str | None) -> tuple[str, int, str]:
	"""(short label, grade order, section letter): «الصف الثاني عشر - ب (2026-2027)» → 12ب."""
	clean = re.sub(r"\s*\(.*?\)\s*$", "", label or "").strip()
	letter = clean.rsplit("-", 1)[1].strip() if "-" in clean else ""
	order, grade = _grade_of(program, clean)
	if letter and len(letter) <= 3:
		return f"{grade}{letter}" if grade != "ت" else f"ت{letter}", order, letter
	return clean[:10], order, letter


def _short_course(course: str) -> tuple[str, str]:
	"""(the subject without its grade, a word or two for the cell)."""
	subject = re.sub(r"\s*-\s*G?\d+\s*$", "", course or "").strip()
	if subject in SUBJECT_SHORT:
		return subject, SUBJECT_SHORT[subject]
	words = [w for w in subject.split() if w not in ("التربية", "اللغة")]
	short = (words[0] if words else subject)
	if short.startswith("ال") and len(short) > 4:
		short = short[2:]
	return subject, short[:9]


# --- what the sheet is drawn from ----------------------------------------------


def _groups() -> list[dict]:
	rows = frappe.get_all(
		"Student Group",
		filters=apply_period({"disabled": 0}, "Student Group"),
		fields=["name", "student_group_name", "program", "ms_homeroom_instructor"],
		limit_page_length=0,
	)
	students = {}
	for r in frappe.db.sql(
		"""select parent, count(*) from `tabStudent Group Student`
		where active = 1 group by parent""",
	):
		students[r[0]] = cint(r[1])
	clocks = tg.clocks_for([r.name for r in rows])
	out = []
	for r in rows:
		label = r.student_group_name or r.name
		short, order, letter = _short_group(label, r.program)
		out.append({
			"id": r.name,
			"label": re.sub(r"\s*\(.*?\)\s*$", "", label).strip(),
			"short": short,
			"program": r.program or "",
			"gradeOrder": order,
			"letter": letter,
			"homeroom": r.ms_homeroom_instructor or "",
			"students": students.get(r.name, 0),
			"courses": tg._courses_for_group(r.name),
			"clock": [
				{"order": cint(p["order"]), "from": p["from"], "to": p["to"]}
				for p in clocks.get(r.name, [])
				if not p.get("isBreak")
			],
		})
	out.sort(key=lambda g: (g["gradeOrder"], g["letter"], g["label"]))
	return out


def _slots() -> list[dict]:
	return [
		{
			"id": r.name,
			"day": r.day,
			"period": cint(r.period_order),
			"group": r.student_group,
			"course": r.course,
			"instructor": r.instructor or "",
			"room": r.room or "",
		}
		for r in frappe.get_all(
			"MS Timetable Slot",
			filters={"active": 1},
			fields=["name", "day", "period_order", "student_group", "course", "instructor", "room"],
			order_by="day, period_order",
			limit_page_length=0,
		)
	]


def _limits() -> dict:
	"""Daily limits the timetabler set, per section and subject; 2 otherwise."""
	out = {}
	for key, value in frappe.db.sql(
		"select defkey, defvalue from tabDefaultValue where defkey like %s", "ms_daily_limit:%"
	):
		_, group, course = key.split(":", 2)
		if cint(value) > 0:
			out[f"{group}#{course}"] = cint(value)
	return out


def _generation_summary(groups: list[str]) -> dict:
	"""How far the generated lessons already reach, for the generate dialog."""
	since = getdate(today())
	future = frappe.db.count(
		"Course Schedule", {"schedule_date": [">=", since], "docstatus": ["<", 2]}
	)
	last = frappe.get_all(
		"Course Schedule",
		filters={"schedule_date": [">=", since], "docstatus": ["<", 2]},
		fields=["schedule_date"],
		order_by="schedule_date desc",
		limit=1,
	)
	audiences: dict[str, int] = {}
	if future:
		for r in frappe.db.sql(
			"""select coalesce(nullif(ms_audience, ''), 'draft'), count(*) from `tabCourse Schedule`
			where schedule_date >= %s and docstatus < 2 group by 1""",
			since,
		):
			audiences[r[0]] = cint(r[1])
	ranges = [r for r in (tg._resolve_range(g, None, None) for g in groups[:60]) if r[0] and r[1]]
	first = min(getdate(r[0]) for r in ranges) if ranges else None
	last_day = max(getdate(r[1]) for r in ranges) if ranges else None
	# From today, so the past stays as it was taught — unless the term is over.
	start = max(first, since) if first and last_day and last_day >= since else first
	return {
		"lessons": future,
		"generatedTo": str(last[0].schedule_date) if last else "",
		"audience": max(audiences, key=audiences.get) if audiences else "draft",
		"defaultFrom": str(start or ""),
		"defaultTo": str(last_day or ""),
		"audiences": [{"value": k, "label": v} for k, v in tg.AUDIENCES.items()],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def load(persona: str = None):
	"""Everything the sheet needs, in one call."""
	periods, working_days = tg.grid_periods()
	groups = _groups()
	slots = _slots()

	teaching: dict[str, set] = {}
	for s in slots:
		if s["instructor"]:
			teaching.setdefault(s["instructor"], set()).add(s["course"])
	homeroom: dict[str, list] = {}
	for g in groups:
		if g["homeroom"]:
			homeroom.setdefault(g["homeroom"], []).append(g["id"])
	teachers = [
		{
			"id": i.name,
			"name": i.instructor_name or i.name,
			"quota": cint(i.ms_weekly_quota),
			"homeroomOf": homeroom.get(i.name, []),
			"courses": sorted(teaching.get(i.name, [])),
		}
		for i in frappe.get_all(
			"Instructor",
			filters={"status": "Active"},
			fields=["name", "instructor_name", "ms_weekly_quota"],
			order_by="instructor_name",
			limit_page_length=0,
		)
	]
	# A slot naming a teacher since made inactive still has to be drawn.
	known = {t["id"] for t in teachers}
	for missing in sorted({s["instructor"] for s in slots if s["instructor"] and s["instructor"] not in known}):
		teachers.append({
			"id": missing,
			"name": frappe.db.get_value("Instructor", missing, "instructor_name") or missing,
			"quota": cint(frappe.db.get_value("Instructor", missing, "ms_weekly_quota")),
			"homeroomOf": homeroom.get(missing, []),
			"courses": sorted(teaching.get(missing, [])),
			"inactive": True,
		})

	course_ids = {c for g in groups for c in g["courses"]} | {s["course"] for s in slots if s["course"]}
	courses = {}
	for c in sorted(course_ids):
		subject, short = _short_course(c)
		courses[c] = {"label": c, "subject": subject, "short": short}

	return {
		"days": [{"value": k, "label": v} for k, v in sched.WEEKDAYS if k in working_days],
		"periods": [{"order": cint(p["order"]), "from": p["from"], "to": p["to"]} for p in periods],
		"groups": groups,
		"teachers": teachers,
		"courses": courses,
		"rooms": [
			{"id": r.name, "label": r.room_name or r.name}
			for r in frappe.get_all("Room", fields=["name", "room_name"], order_by="name")
		],
		"slots": slots,
		"limits": _limits(),
		"defaultLimit": tg.DEFAULT_DAILY_LIMIT,
		"generation": _generation_summary([g["id"] for g in groups]),
		"job": _job(),
	}


# --- the rules -------------------------------------------------------------------


def validate(slots: list[dict], groups: dict, teachers: dict, limits: dict) -> list[dict]:
	"""Every problem with a proposed week, each naming the cells it involves.

	`errors` stop a save; `warnings` are shown and allowed. Mirrors
	`checkWeek` in the page, rule for rule.
	"""
	problems: list[dict] = []
	day_label = dict(sched.WEEKDAYS)

	def at(s):
		return f"{day_label.get(s['day'], s['day'])} ح{s['period']}"

	def clock(s):
		for p in (groups.get(s["group"]) or {}).get("clock", []):
			if p["order"] == s["period"]:
				return p["from"], p["to"]
		return None

	def gname(g):
		return (groups.get(g) or {}).get("short") or g

	def tname(t):
		return (teachers.get(t) or {}).get("name") or t

	for s in slots:
		if s["group"] not in groups:
			problems.append({"kind": "group", "level": "error", "keys": [s["k"]], "message": f"شعبة غير معروفة ({at(s)})"})
		elif not clock(s):
			problems.append({
				"kind": "time", "level": "error", "keys": [s["k"]],
				"message": f"{gname(s['group'])}: لا توجد الحصة {s['period']} في يومها الدراسي ({at(s)})",
			})
		if not s.get("course"):
			problems.append({"kind": "course", "level": "error", "keys": [s["k"]], "message": f"{gname(s['group'])}: بلا مادة ({at(s)})"})
		elif s["group"] in groups and s["course"] not in groups[s["group"]]["courses"]:
			problems.append({
				"kind": "course", "level": "warning", "keys": [s["k"]],
				"message": f"{gname(s['group'])}: «{s['course']}» ليست من مواد صفها ({at(s)})",
			})
		if not s.get("instructor"):
			problems.append({
				"kind": "teacher", "level": "warning", "keys": [s["k"]],
				"message": f"{gname(s['group'])}: حصة بلا معلم — لن تُولَّد ({at(s)})",
			})

	# A section in two lessons at once.
	by_cell: dict[tuple, list] = {}
	for s in slots:
		by_cell.setdefault((s["group"], s["day"], s["period"]), []).append(s)
	for (group, _, _), items in by_cell.items():
		if len(items) > 1:
			who = "، ".join(sorted({tname(i["instructor"]) for i in items if i["instructor"]}))
			problems.append({
				"kind": "group", "level": "error", "keys": [i["k"] for i in items],
				"message": f"{gname(group)} في {len(items)} حصص معاً ({at(items[0])}): {who}",
			})

	# A teacher, or a room, in two places at once — by the clock each section runs.
	for field, kind, label in (("instructor", "teacher", tname), ("room", "room", lambda r: r)):
		by_owner: dict[tuple, list] = {}
		for s in slots:
			if s.get(field) and clock(s):
				by_owner.setdefault((s[field], s["day"]), []).append(s)
		for (owner, _), items in by_owner.items():
			items = sorted(items, key=lambda s: clock(s)[0])
			for i, a in enumerate(items):
				fa, ta = clock(a)
				for b in items[i + 1:]:
					fb, tb = clock(b)
					if fb >= ta:
						break
					if sched.overlaps(f"{fa}:00", f"{ta}:00", f"{fb}:00", f"{tb}:00"):
						problems.append({
							"kind": kind, "level": "error", "keys": [a["k"], b["k"]],
							"message": f"{label(owner)} في {gname(a['group'])} و{gname(b['group'])} معاً ({at(a)})",
						})

	# النصاب.
	counts: dict[str, list] = {}
	for s in slots:
		if s.get("instructor"):
			counts.setdefault(s["instructor"], []).append(s["k"])
	for t, keys in counts.items():
		quota = cint((teachers.get(t) or {}).get("quota"))
		if quota and len(keys) > quota:
			problems.append({
				"kind": "quota", "level": "error", "keys": keys,
				"message": f"{tname(t)}: {len(keys)} حصة تتجاوز النصاب ({quota})",
			})

	# One subject too many times in a day.
	per_day: dict[tuple, list] = {}
	for s in slots:
		if s.get("course"):
			per_day.setdefault((s["group"], s["course"], s["day"]), []).append(s)
	for (group, course, day), items in per_day.items():
		limit = cint(limits.get(f"{group}#{course}")) or tg.DEFAULT_DAILY_LIMIT
		if len(items) > limit:
			problems.append({
				"kind": "daily", "level": "warning", "keys": [i["k"] for i in items],
				"message": f"{gname(group)}: {_short_course(course)[0]} {len(items)} مرات {day_label.get(day, day)} (الحد {limit})",
			})
	return problems


def _proposed(slots) -> list[dict]:
	out = []
	for i, s in enumerate(parse_json_arg(slots) or []):
		if not s.get("group") or not s.get("day") or not cint(s.get("period")):
			continue
		out.append({
			"k": str(s.get("k") or s.get("id") or i),
			"id": s.get("id") or "",
			"day": s["day"],
			"period": cint(s["period"]),
			"group": s["group"],
			"course": s.get("course") or "",
			"instructor": s.get("instructor") or "",
			"room": s.get("room") or "",
		})
	return out


def _meta() -> tuple[dict, dict]:
	groups = {g["id"]: g for g in _groups()}
	teachers = {
		i.name: {"name": i.instructor_name or i.name, "quota": cint(i.ms_weekly_quota)}
		for i in frappe.get_all(
			"Instructor", fields=["name", "instructor_name", "ms_weekly_quota"], limit_page_length=0
		)
	}
	return groups, teachers


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def check(slots: str | list, persona: str = None):
	"""The save's checks without the save."""
	groups, teachers = _meta()
	problems = validate(_proposed(slots), groups, teachers, _limits())
	return {"problems": problems, "errors": sum(1 for p in problems if p["level"] == "error")}


def _differs(s: dict, old) -> bool:
	"""Whether a proposed cell is new, or changes the stored lesson it came from."""
	if not old:
		return True
	return not (
		old.day == s["day"] and cint(old.period_order) == s["period"]
		and old.student_group == s["group"] and old.course == s["course"]
		and (old.instructor or "") == s["instructor"] and (old.room or "") == s["room"]
	)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save(changes: str | list, deletes: str | list = None, limits: str | dict = None, persona: str = None):
	"""Store what changed on the sheet. Refused while it has an error.

	The page sends only its changes — the cells it added or edited, and the
	stored lessons it removed — and they are laid over the week as it stands
	now. Sending the whole week instead would let a second tab, opened before
	another saved, delete everything added since it loaded. The checks run on
	the week as it will be, so a cell clashing with a lesson saved elsewhere in
	the meantime is still caught.
	"""
	if _job().get("state") == "running":
		return fail(
			"Lessons are being generated — wait for it to finish.",
			"يجري توليد الحصص الآن — انتظر حتى ينتهي ثم احفظ.",
		)
	upserts = _proposed(changes)
	removing = {str(d) for d in (parse_json_arg(deletes) or []) if d}
	new_limits = parse_json_arg(limits, {}) or {}
	all_limits = {**_limits(), **{k: cint(v) for k, v in new_limits.items() if cint(v) > 0}}
	existing = {
		r.name: r
		for r in frappe.get_all(
			"MS Timetable Slot",
			filters={"active": 1},
			fields=["name", "day", "period_order", "student_group", "course", "instructor", "room"],
			limit_page_length=0,
		)
	}
	edits = {u["id"]: u for u in upserts if u["id"] and u["id"] in existing}
	week = []
	for name, old in existing.items():
		if name in removing:
			continue
		week.append(edits.get(name) or {
			"k": name, "id": name, "day": old.day, "period": cint(old.period_order),
			"group": old.student_group, "course": old.course,
			"instructor": old.instructor or "", "room": old.room or "",
		})
	# New cells — and an edit of a lesson someone else has since deleted,
	# which is put back as new rather than lost.
	inserts = [dict(u, id="") for u in upserts if u["id"] not in edits]
	week += inserts

	groups, teachers = _meta()
	problems = validate(week, groups, teachers, all_limits)
	# An error stops the save only when this edit is part of it. A clash the
	# week already had, between cells nobody touched, is shown but does not
	# hold every other change in the school hostage.
	changed = {u["k"] for u in upserts}
	errors = []
	for p in problems:
		if p["level"] != "error":
			continue
		if changed.intersection(p["keys"]):
			errors.append(p)
		else:
			p["legacy"] = True
	if errors:
		return fail(
			f"The week has {len(errors)} error(s)",
			f"الجدول فيه {len(errors)} خطأ — عالجها قبل الحفظ: " + "؛ ".join(p["message"] for p in errors[:4]),
			data={"problems": problems},
		)
	year, term = get_default_academic_year(), get_default_academic_term()
	touched_groups: set[str] = set()
	touched_teachers: set[str] = set()
	created = updated = removed = 0

	def times(s):
		for p in groups[s["group"]]["clock"]:
			if p["order"] == s["period"]:
				return f"{p['from']}:00", f"{p['to']}:00"
		return None, None

	for name in removing:
		old = existing.get(name)
		if not old:
			continue
		touched_groups.add(old.student_group)
		touched_teachers.add(old.instructor or "")
		frappe.delete_doc("MS Timetable Slot", name, ignore_permissions=True, force=True)
		removed += 1

	for s in list(edits.values()) + inserts:
		old = existing.get(s["id"]) if s["id"] else None
		if old:
			if not _differs(s, old):
				continue
			touched_groups |= {old.student_group, s["group"]}
			touched_teachers |= {old.instructor or "", s["instructor"]}
			doc = frappe.get_doc("MS Timetable Slot", old.name)
			updated += 1
		else:
			doc = frappe.new_doc("MS Timetable Slot")
			doc.academic_year, doc.academic_term, doc.active = year, term, 1
			touched_groups.add(s["group"])
			touched_teachers.add(s["instructor"])
			created += 1
		doc.student_group = s["group"]
		doc.day = s["day"]
		doc.period_order = s["period"]
		doc.from_time, doc.to_time = times(s)
		doc.course = s["course"]
		doc.instructor = s["instructor"] or None
		doc.room = s["room"] or None
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)

	tg._save_daily_limits(new_limits)
	for group in touched_groups:
		if group:
			sync_group_instructors(group)
	# The class plans follow the sheet, as they follow the teacher screen.
	by_teacher: dict[str, list] = {}
	for s in week:
		if s["instructor"] in touched_teachers and s["instructor"]:
			by_teacher.setdefault(s["instructor"], []).append({"studentGroup": s["group"], "course": s["course"]})
	for teacher, rows in by_teacher.items():
		tg._sync_plans(teacher, rows, all_limits)
	frappe.db.commit()

	# Lessons already generated follow the saved week — in the background, as
	# a whole school's term will not finish inside a request.
	sync = None
	groups_with_lessons = [
		g for g in sorted(g for g in touched_groups if g)
		if frappe.db.exists("Course Schedule", {"student_group": g, "schedule_date": [">=", today()], "docstatus": ["<", 2]})
	]
	if groups_with_lessons:
		sync = _start_job("sync", groups_with_lessons)

	return {
		"created": created,
		"updated": updated,
		"removed": removed,
		"unchanged": len(existing) - updated - removed,
		"groups": len([g for g in touched_groups if g]),
		"warnings": len(problems),
		"job": sync,
		"slots": _slots(),
		"message_en": "Timetable saved.",
		"message_ar": "تم حفظ الجدول"
		+ (f" — تُحدَّث الحصص المولّدة لـ {len(groups_with_lessons)} شعبة في الخلفية." if sync else "."),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def set_quota(instructor: str, quota: int = 0, persona: str = None):
	"""A teacher's weekly quota, edited from the sheet's النصاب column."""
	if not frappe.db.exists("Instructor", instructor):
		return fail("Teacher not found", "لم يتم العثور على المعلم")
	quota = max(0, cint(quota))
	frappe.db.set_value("Instructor", instructor, "ms_weekly_quota", quota)
	frappe.db.commit()
	return {"instructor": instructor, "quota": quota}


# --- generation, in the background ---------------------------------------------------


STALE_MINUTES = 20


def _job() -> dict:
	"""The last generation's progress. A run whose worker went quiet for
	twenty minutes reads as stopped, so a crashed worker cannot hold the
	sheet's save button hostage."""
	state = frappe.cache.get_value(JOB_KEY) or {}
	if state.get("state") == "running" and state.get("updatedAt"):
		quiet = (now_datetime() - get_datetime(state["updatedAt"])).total_seconds() / 60
		if quiet > STALE_MINUTES:
			state["state"] = "stalled"
	return state


def _put(state: dict) -> None:
	state["updatedAt"] = str(now_datetime())
	frappe.cache.set_value(JOB_KEY, state, expires_in_sec=3 * 86400)


def _start_job(mode: str, groups: list[str], start=None, end=None, audience=None) -> dict:
	state = {
		"state": "running",
		"mode": mode,
		"total": len(groups),
		"done": 0,
		"current": "",
		"created": 0,
		"removed": 0,
		"kept": 0,
		"skipped": 0,
		"errors": [],
		"from": str(start or ""),
		"to": str(end or ""),
		"audience": audience or "",
		"by": frappe.session.user,
		"startedAt": str(now_datetime()),
	}
	_put(state)
	frappe.enqueue(
		"match_schools.api.school_timetable.run_job",
		queue="long",
		timeout=3 * 3600,
		mode=mode,
		groups=groups,
		start=str(start or ""),
		end=str(end or ""),
		audience=audience or "",
		enqueue_after_commit=False,
	)
	return state


def run_job(mode: str, groups: list[str], start: str = "", end: str = "", audience: str = "") -> None:
	"""Generate, or re-sync, section by section — one section's failure is
	reported and the rest carry on."""
	state = _job() or {"state": "running", "errors": []}
	labels = tg._group_labels(set(groups))
	for i, group in enumerate(groups):
		state["current"] = labels.get(group, group)
		_put(state)
		try:
			if mode == "generate":
				slots = [dict(s, student_group=group) for s in tg._slot_rows({"student_group": group})]
				a, b = (start, end) if start and end else tg._resolve_range(group, None, None)
				if a and b and slots:
					result = tg.regenerate_lessons(slots, {"student_group": group}, a, b, audience or "draft")
					tg._remember_until({"student_group": group}, b)
					tg._remember_audience({"student_group": group}, audience or "draft")
				else:
					result = None
			else:
				result = tg.resync_lessons({"student_group": group})
			sync_group_instructors(group)
			frappe.db.commit()
			if result:
				state["created"] += cint(result.get("created"))
				state["removed"] += cint(result.get("removed"))
				state["kept"] += cint(result.get("keptAttended"))
				state["skipped"] += len(result.get("skipped") or [])
				reasons = state.setdefault("reasons", {})
				for item in result.get("skipped") or []:
					why = (item.get("reason") or "")[:120]
					reasons[why] = reasons.get(why, 0) + 1
				if result.get("error"):
					state["errors"].append(f"{labels.get(group, group)}: {result['error']}")
		except Exception as exc:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "School timetable generation")
			state["errors"].append(f"{labels.get(group, group)}: {str(exc)[:140]}")
		state["done"] = i + 1
		_put(state)
	state["state"] = "done"
	state["current"] = ""
	state["finishedAt"] = str(now_datetime())
	_put(state)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def generate(
	from_date: str = None,
	to_date: str = None,
	audience: str = "draft",
	groups: str | list = None,
	persona: str = None,
):
	"""Turn the saved week into dated lessons for every section (or the ones named)."""
	if _job().get("state") == "running":
		return fail("A generation is already running.", "يجري توليد الحصص الآن — انتظر حتى ينتهي.")
	if audience not in tg.AUDIENCES:
		return fail("Unknown audience", "جمهور غير معروف")
	if bool(from_date) != bool(to_date):
		return fail("Give both dates or neither", "حدّد تاريخ البداية والنهاية معاً")
	if from_date and getdate(to_date) < getdate(from_date):
		return fail("The end date is before the start", "تاريخ النهاية قبل تاريخ البداية")
	chosen = parse_json_arg(groups, None) or None
	with_slots = sorted(set(
		frappe.get_all("MS Timetable Slot", filters={"active": 1}, pluck="student_group", distinct=True)
	))
	targets = [g for g in with_slots if not chosen or g in chosen]
	if not targets:
		return fail("No saved week to generate", "لا يوجد جدول محفوظ لتوليده — احفظ الجدول أولاً")
	return {"job": _start_job("generate", targets, from_date, to_date, audience)}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def job_status(persona: str = None):
	state = _job()
	if state.get("state") == "done":
		state["generation"] = _generation_summary([g["id"] for g in _groups()])
	return state
