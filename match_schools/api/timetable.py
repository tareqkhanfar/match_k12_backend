# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Automatic timetable generation.

A plan states what a class needs: the working days, the daily period grid, and
how many periods a week each subject wants (optionally with a teacher and a
room). Generation places those lessons on the grid without ever double-booking
a teacher or a room, and without stacking a subject on one day.

The search is a backtracking placement over the hardest-to-place subjects
first. That is enough for a school week — a class has perhaps 35 slots — and
it either finds a valid timetable or reports exactly what it could not place,
rather than silently producing a broken one.
"""

import random

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, today

from match_schools.api.scheduling import overlaps
from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)

DAY_AR = {
	"Sunday": "الأحد",
	"Monday": "الإثنين",
	"Tuesday": "الثلاثاء",
	"Wednesday": "الأربعاء",
	"Thursday": "الخميس",
	"Friday": "الجمعة",
	"Saturday": "السبت",
}
def _hhmmss(value) -> str:
	"""Normalise a time to HH:MM:SS.

	Frappe returns a timedelta that stringifies as "8:00:00" — no leading zero,
	so "9:55:00" sorts after "10:40:00" and Education rejects the range.
	"""
	if value in (None, ""):
		return ""
	text = str(value).strip()
	parts = text.split(":")
	if len(parts) < 2:
		return text
	hh = parts[0].zfill(2)
	mm = parts[1].zfill(2)
	ss = (parts[2].split(".")[0] if len(parts) > 2 else "00").zfill(2)
	return f"{hh}:{mm}:{ss}"


DAY_INDEX = {
	"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
	"Friday": 4, "Saturday": 5, "Sunday": 6,
}

# A sensible default bell schedule, so a new plan is usable immediately.
DEFAULT_PERIODS = [
	("الحصة الأولى", 1, "08:00:00", "08:45:00", 0),
	("الحصة الثانية", 2, "08:50:00", "09:35:00", 0),
	("استراحة", 3, "09:35:00", "09:55:00", 1),
	("الحصة الثالثة", 4, "09:55:00", "10:40:00", 0),
	("الحصة الرابعة", 5, "10:45:00", "11:30:00", 0),
	("الحصة الخامسة", 6, "11:35:00", "12:20:00", 0),
	("الحصة السادسة", 7, "12:25:00", "13:10:00", 0),
]

# Ordinal names, so a generated day reads the way a school writes it.
PERIOD_ORDINALS = [
	"الأولى", "الثانية", "الثالثة", "الرابعة", "الخامسة", "السادسة",
	"السابعة", "الثامنة", "التاسعة", "العاشرة", "الحادية عشرة", "الثانية عشرة",
]

DEFAULT_WORKING_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]

# The school day as the school actually runs it, held once rather than being
# retyped for every section. The builder loads these as its starting point and
# a timetabler may still override them for a particular class.
DAY_SHAPE_KEYS = {
	"count": ("ms_periods_per_day", 7),
	"minutes": ("ms_period_minutes", 45),
	"gap": ("ms_period_gap", 5),
	"break_after": ("ms_break_after_period", 2),
	"break_minutes": ("ms_break_minutes", 20),
}
DAY_START_KEY = "ms_day_start"
WORKING_DAYS_KEY = "ms_working_days"


def school_day_shape() -> dict:
	"""The school's default day, falling back to a sensible one."""
	shape = {}
	for field, (key, fallback) in DAY_SHAPE_KEYS.items():
		stored = frappe.db.get_default(key)
		# 0 is meaningful for break_after ("no break"), so only an unset value
		# falls back — `or` would quietly restore the default.
		shape[field] = cint(stored) if stored not in (None, "") else fallback

	shape["start"] = _hhmmss(frappe.db.get_default(DAY_START_KEY) or "08:00:00")

	stored_days = frappe.db.get_default(WORKING_DAYS_KEY)
	days = [d.strip() for d in (stored_days or "").split(",") if d.strip()]
	shape["working_days"] = days or list(DEFAULT_WORKING_DAYS)
	return shape


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_day_shape(persona: str = None):
	"""The school's default working days and period pattern."""
	shape = school_day_shape()
	# `build_periods`, not the endpoint: `preview_periods` is wrapped by
	# ms_endpoint and would return an envelope nested inside this one.
	periods = build_periods(
		count=shape["count"],
		minutes=shape["minutes"],
		start=shape["start"],
		gap=shape["gap"],
		break_after=shape["break_after"],
		break_minutes=shape["break_minutes"],
	)
	return {
		**shape,
		"days": [{"code": c, "label": l} for c, l in DAY_AR.items()],
		"periods": [
			{
				"name": p["period_name"],
				"order": p["period_order"],
				"from_time": p["from_time"],
				"to_time": p["to_time"],
				"is_break": bool(p["is_break"]),
			}
			for p in periods
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_day_shape(
	count: int = None,
	minutes: int = None,
	start: str = None,
	gap: int = None,
	break_after: int = None,
	break_minutes: int = None,
	working_days: str | list = None,
	persona: str = None,
):
	"""Set the school-wide default day. Existing timetables are untouched."""
	values = {
		"count": count,
		"minutes": minutes,
		"gap": gap,
		"break_after": break_after,
		"break_minutes": break_minutes,
	}
	for field, value in values.items():
		if value is None:
			continue
		key, _fallback = DAY_SHAPE_KEYS[field]
		frappe.db.set_default(key, cint(value), parent="__default")

	if start:
		frappe.db.set_default(DAY_START_KEY, _hhmmss(start), parent="__default")

	if working_days is not None:
		days = parse_json_arg(working_days, []) or []
		if isinstance(days, str):
			days = [d.strip() for d in days.split(",") if d.strip()]
		days = [d for d in days if d in DAY_AR]
		if not days:
			return fail(
				message_en="Choose at least one working day.",
				message_ar="اختر يوم دوام واحداً على الأقل.",
			)
		frappe.db.set_default(WORKING_DAYS_KEY, ",".join(days), parent="__default")

	frappe.db.commit()
	return {
		**school_day_shape(),
		"message_en": "Default school day saved.",
		"message_ar": "تم حفظ إعدادات اليوم الدراسي.",
	}


def build_periods(
	*,
	count: int = 7,
	minutes: int = 45,
	start: str = "08:00:00",
	gap: int = 5,
	break_after: int = 2,
	break_minutes: int = 20,
) -> list[dict]:
	"""Lay out a school day from the few facts that actually vary.

	A school describes its day as "seven periods of forty-five minutes, break
	after the second" — not as a list of timestamps. This turns that
	description into the period rows the plan stores, so the times stay
	consistent instead of being typed in one by one.

	`break_after` is a teaching-period number; 0 means no break at all.
	"""
	count = max(1, cint(count) or 7)
	minutes = max(5, cint(minutes) or 45)
	gap = max(0, cint(gap))
	break_after = max(0, cint(break_after))
	break_minutes = max(0, cint(break_minutes))

	# Times are computed in minutes from midnight; a datetime would drag
	# timezones into something that is only ever a clock reading.
	parts = (_hhmmss(start) or "08:00:00").split(":")
	cursor = cint(parts[0]) * 60 + cint(parts[1])

	def clock(total: int) -> str:
		total %= 24 * 60
		return f"{total // 60:02d}:{total % 60:02d}:00"

	rows: list[dict] = []
	order = 0
	for n in range(1, count + 1):
		order += 1
		rows.append(
			{
				"period_name": f"الحصة {PERIOD_ORDINALS[n - 1]}"
				if n <= len(PERIOD_ORDINALS)
				else f"الحصة {n}",
				"period_order": order,
				"from_time": clock(cursor),
				"to_time": clock(cursor + minutes),
				"is_break": 0,
			}
		)
		cursor += minutes

		if break_after and n == break_after and n < count and break_minutes:
			order += 1
			rows.append(
				{
					"period_name": "استراحة",
					"period_order": order,
					"from_time": clock(cursor),
					"to_time": clock(cursor + break_minutes),
					"is_break": 1,
				}
			)
			cursor += break_minutes
		elif n < count:
			cursor += gap

	return rows


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def preview_periods(
	count: int = 7,
	minutes: int = 45,
	start: str = "08:00:00",
	gap: int = 5,
	break_after: int = 2,
	break_minutes: int = 20,
	persona: str = None,
):
	"""The day these settings produce, without saving anything."""
	periods = build_periods(
		count=count,
		minutes=minutes,
		start=start,
		gap=gap,
		break_after=break_after,
		break_minutes=break_minutes,
	)
	return {
		"periods": [
			{
				"name": p["period_name"],
				"order": p["period_order"],
				"from_time": p["from_time"],
				"to_time": p["to_time"],
				"is_break": bool(p["is_break"]),
			}
			for p in periods
		],
		"teaching": sum(1 for p in periods if not p["is_break"]),
		"ends_at": periods[-1]["to_time"] if periods else start,
	}


# --- Plans -----------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def list_plans(student_group: str = None, persona: str = None):
	"""Every timetable plan, newest first."""
	filters = {}
	if student_group:
		filters["student_group"] = student_group

	rows = frappe.get_all(
		"MS Timetable Plan",
		filters=filters,
		fields=[
			"name", "plan_name", "student_group", "academic_year", "academic_term",
			"status", "lessons_created", "generated_on", "modified",
		],
		order_by="modified desc",
		limit=100,
	)
	return [
		{
			"id": r.name,
			"name": r.plan_name,
			"student_group": r.student_group,
			"academic_year": r.academic_year,
			"academic_term": r.academic_term,
			"status": r.status,
			"lessons": cint(r.lessons_created),
			"generated_on": str(r.generated_on or ""),
		}
		for r in rows
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def get_plan(plan: str, persona: str = None):
	"""One plan with its periods and subject loads."""
	doc = frappe.get_doc("MS Timetable Plan", plan)
	return _plan_payload(doc)


def _plan_payload(doc) -> dict:
	return {
		"id": doc.name,
		"name": doc.plan_name,
		"student_group": doc.student_group,
		"academic_year": doc.academic_year,
		"academic_term": doc.academic_term,
		"status": doc.status,
		"working_days": [d.strip() for d in (doc.working_days or "").split(",") if d.strip()],
		"lessons": cint(doc.lessons_created),
		"notes": doc.notes,
		"periods": [
			{
				"name": p.period_name,
				"order": cint(p.period_order),
				"from_time": _hhmmss(p.from_time),
				"to_time": _hhmmss(p.to_time),
				"is_break": bool(p.is_break),
			}
			for p in sorted(doc.periods, key=lambda x: cint(x.period_order))
		],
		"subjects": [
			{
				"course": s.course,
				"periods_per_week": cint(s.periods_per_week),
				"instructor": s.instructor,
				"preferred_room": s.preferred_room,
				"max_per_day": cint(s.max_per_day) or 2,
			}
			for s in doc.subject_loads
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_plan(payload: str | dict, persona: str = None):
	"""Create or update a timetable plan."""
	data = parse_json_arg(payload) or {}
	if not data.get("student_group"):
		return fail(message_en="Choose a class.", message_ar="اختر الشعبة.")

	plan_id = data.get("id")
	doc = (
		frappe.get_doc("MS Timetable Plan", plan_id)
		if plan_id
		else frappe.new_doc("MS Timetable Plan")
	)

	group = frappe.db.get_value(
		"Student Group",
		data["student_group"],
		["student_group_name", "academic_year", "academic_term"],
		as_dict=True,
	)

	doc.plan_name = data.get("name") or f"جدول {group.student_group_name if group else data['student_group']}"
	doc.student_group = data["student_group"]
	doc.academic_year = data.get("academic_year") or (group.academic_year if group else None)
	doc.academic_term = data.get("academic_term") or (group.academic_term if group else None)
	doc.working_days = ",".join(
		data.get("working_days") or ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]
	)
	doc.notes = data.get("notes")

	periods = data.get("periods")
	if periods is not None or not doc.periods:
		doc.set("periods", [])
		for p in periods or [
			{
				"period_name": n,
				"period_order": o,
				"from_time": f,
				"to_time": t,
				"is_break": b,
			}
			for n, o, f, t, b in DEFAULT_PERIODS
		]:
			doc.append(
				"periods",
				{
					"period_name": p.get("period_name") or p.get("name"),
					"period_order": cint(p.get("period_order") or p.get("order")),
					"from_time": p.get("from_time"),
					"to_time": p.get("to_time"),
					"is_break": cint(p.get("is_break")),
				},
			)

	if data.get("subjects") is not None:
		doc.set("subject_loads", [])
		for s in data["subjects"]:
			if not s.get("course"):
				continue
			doc.append(
				"subject_loads",
				{
					"course": s["course"],
					"periods_per_week": cint(s.get("periods_per_week")) or 1,
					"instructor": s.get("instructor"),
					"preferred_room": s.get("preferred_room"),
					"max_per_day": cint(s.get("max_per_day")) or 2,
				},
			)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": _plan_payload(doc),
		"message_en": "Plan saved.",
		"message_ar": "تم حفظ خطة الجدول.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_plan(plan: str, persona: str = None):
	frappe.delete_doc("MS Timetable Plan", plan, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": plan},
		"message_en": "Plan deleted.",
		"message_ar": "تم حذف الخطة.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def plan_defaults(student_group: str = None, persona: str = None):
	"""Sensible starting values, including the subjects the class is taught."""
	subjects = []
	if student_group:
		group = frappe.db.get_value(
			"Student Group", student_group, ["program", "course"], as_dict=True
		)
		courses = set()
		if group and group.course:
			courses.add(group.course)
		if group and group.program:
			courses |= {
				r.course
				for r in frappe.get_all(
					"Program Course",
					filters={"parent": group.program, "parenttype": "Program"},
					fields=["course"],
				)
				if r.course
			}
		# Whoever already teaches the class is the natural default teacher.
		for course in sorted(courses):
			instructor = frappe.db.get_value(
				"Course Schedule",
				{"student_group": student_group, "course": course},
				"instructor",
			)
			subjects.append(
				{
					"course": course,
					"periods_per_week": 3,
					"instructor": instructor,
					"preferred_room": None,
					"max_per_day": 2,
				}
			)

	return {
		"periods": [
			{
				"period_name": n,
				"period_order": o,
				"from_time": f,
				"to_time": t,
				"is_break": bool(b),
			}
			for n, o, f, t, b in DEFAULT_PERIODS
		],
		"working_days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"],
		"subjects": subjects,
		"days": [{"code": c, "label": l} for c, l in DAY_AR.items()],
	}


# --- Generation ------------------------------------------------------------


class Solver:
	"""Places lessons on the weekly grid without clashing.

	Two lessons clash when they share a slot and either the same teacher or the
	same room. Busy maps carry the whole school's existing timetable, so a
	teacher booked with another class is respected too.
	"""

	def __init__(
		self, days, slots, subjects, busy_teacher, busy_room, seed=None, slot_times=None
	):
		self.days = days
		self.slots = slots
		# الحصة تُعرف بوقت بدئها في الشبكة، لكن التعارض يُقاس بفترتها كاملة.
		self.slot_times = slot_times or {s: (s, s) for s in slots}
		self.subjects = subjects
		self.busy_teacher = busy_teacher
		self.busy_room = busy_room
		# Ties are broken at random so pressing "build" again gives a genuinely
		# different week to choose from. Only ties: the ordering that spreads a
		# subject across the week still decides first, so a fresh arrangement is
		# a different valid timetable rather than a worse one.
		self.rng = random.Random(seed)
		# day -> slot -> the placed lesson
		self.grid: dict[str, dict[int, dict]] = {d: {} for d in days}
		self.per_day: dict[str, dict[str, int]] = {d: {} for d in days}

	def _fits(self, subject, day, slot) -> bool:
		if slot in self.grid[day]:
			return False

		course = subject["course"]
		if self.per_day[day].get(course, 0) >= subject["max_per_day"]:
			return False

		frm, to = self.slot_times[slot]
		if _clashes(self.busy_teacher, subject.get("instructor"), day, frm, to):
			return False
		if _clashes(self.busy_room, subject.get("preferred_room"), day, frm, to):
			return False
		return True

	def _place(self, subject, day, slot):
		self.grid[day][slot] = subject
		self.per_day[day][subject["course"]] = self.per_day[day].get(subject["course"], 0) + 1
		span = self.slot_times[slot]
		if subject.get("instructor"):
			self.busy_teacher.setdefault(subject["instructor"], {}).setdefault(
				day, []
			).append(span)
		if subject.get("preferred_room"):
			self.busy_room.setdefault(subject["preferred_room"], {}).setdefault(
				day, []
			).append(span)

	def _unplace(self, subject, day, slot):
		del self.grid[day][slot]
		self.per_day[day][subject["course"]] -= 1
		span = self.slot_times[slot]
		for store, who in (
			(self.busy_teacher, subject.get("instructor")),
			(self.busy_room, subject.get("preferred_room")),
		):
			if not who:
				continue
			spans = store.get(who, {}).get(day)
			if spans and span in spans:
				spans.remove(span)

	def solve(self) -> tuple[dict, list]:
		"""Return (grid, unplaced). Hardest subjects first, then backtrack."""
		# A subject pinned to a teacher and a room has the fewest options, so
		# placing it first keeps the search shallow.
		def difficulty(s):
			return (
				0 if s.get("instructor") else 1,
				0 if s.get("preferred_room") else 1,
				-s["periods_per_week"],
				# Subjects that are equally constrained are ordered differently
				# each run, which is the other half of what makes a rebuild
				# produce a different week.
				self.rng.random(),
			)

		ordered = sorted(self.subjects, key=difficulty)

		# Interleave rather than emitting a subject's whole weekly load at once:
		# placing all of maths before any science fills Sunday with maths and
		# leaves the last subjects fighting over Thursday.
		lessons = []
		remaining = {s["course"]: s["periods_per_week"] for s in ordered}
		while any(remaining.values()):
			for s in ordered:
				if remaining[s["course"]] > 0:
					lessons.append(s)
					remaining[s["course"]] -= 1

		unplaced: list[dict] = []
		if not self._backtrack(lessons, 0, unplaced, budget=[20000]):
			pass
		return self.grid, unplaced

	def _backtrack(self, lessons, index, unplaced, budget) -> bool:
		if index >= len(lessons):
			return True

		budget[0] -= 1
		if budget[0] <= 0:
			# Give up searching and record the rest as unplaced, rather than
			# hanging the request.
			unplaced.extend(lessons[index:])
			return True

		subject = lessons[index]
		# Spread across the week: fewest of this subject already that day, then
		# the least-full day overall, so the grid fills evenly.
		# The random third key only separates days that are otherwise equally
		# good, so spreading the subject across the week is never sacrificed.
		ordered_days = sorted(
			self.days,
			key=lambda d: (
				self.per_day[d].get(subject["course"], 0),
				len(self.grid[d]),
				self.rng.random(),
			),
		)

		# Slots stay in time order — a school fills the day from the first
		# period, and a shuffled order would leave free periods in the middle
		# of the morning. Variation comes from the day ordering above.
		for day in ordered_days:
			for slot in self.slots:
				if not self._fits(subject, day, slot):
					continue
				self._place(subject, day, slot)
				if self._backtrack(lessons, index + 1, unplaced, budget):
					return True
				self._unplace(subject, day, slot)

		# Nowhere for this lesson; note it and carry on with the rest so the
		# report names everything that failed, not just the first one.
		unplaced.append(subject)
		return self._backtrack(lessons, index + 1, unplaced, budget)


def _existing_bookings(exclude_group: str, academic_term: str | None):
	"""Teacher and room bookings from the rest of the school's timetable.

	Stored as intervals per weekday — `{who: {day: [(from, to), …]}}` — not as
	start times.

	Keying by start time was the bug that made "build automatically" produce a
	week the validator then rejected: a school whose other sections run hourly
	from 09:00 shares no start time with a plan of 45-minute periods from
	08:00, so every existing booking looked free to the solver while the
	validator — which compares intervals — saw all of them overlap. The two
	must answer the same question the same way.
	"""
	busy_teacher: dict[str, dict[str, list]] = {}
	busy_room: dict[str, dict[str, list]] = {}

	def add(store, who, day, frm, to):
		if not (who and day and frm and to):
			return
		store.setdefault(who, {}).setdefault(day, []).append((frm, to))

	filters: dict = {"docstatus": ["<", 2]}
	if exclude_group:
		filters["student_group"] = ["!=", exclude_group]
	# Same scope the validator uses: a plan for this term must not be blocked
	# by last year's timetable, which is still on the calendar.
	if academic_term:
		# شعبةٌ بلا فصل تبقى ضمن الحساب: إسقاطها يجعل الحلّال يضع حصةً على
		# معلّم مشغول عندها دون أن يدري.
		groups = [
			g
			for g in frappe.get_all(
				"Student Group",
				filters={"academic_term": ["in", [academic_term, "", None]]},
				pluck="name",
			)
			if g != exclude_group
		]
		filters["student_group"] = ["in", groups or [""]]

	rows = frappe.get_all(
		"Course Schedule",
		filters=filters,
		fields=["instructor", "room", "schedule_date", "from_time", "to_time"],
		limit_page_length=0,
	)
	for r in rows:
		if not r.schedule_date:
			continue
		day = _weekday_name(r.schedule_date)
		frm, to = _hhmmss(r.from_time), _hhmmss(r.to_time)
		add(busy_teacher, r.instructor, day, frm, to)
		add(busy_room, r.room, day, frm, to)

	# Education validates a lesson's room against Assessment Plan too, so an
	# exam booking blocks the room just as another lesson would.
	for r in frappe.get_all(
		"Assessment Plan",
		filters={"docstatus": ["<", 2]},
		fields=["room", "supervisor", "schedule_date", "from_time", "to_time"],
		limit_page_length=0,
	):
		if not r.schedule_date:
			continue
		day = _weekday_name(r.schedule_date)
		frm, to = _hhmmss(r.from_time), _hhmmss(r.to_time)
		add(busy_room, r.room, day, frm, to)
		add(busy_teacher, r.supervisor, day, frm, to)

	return busy_teacher, busy_room


def _clashes(store: dict, who: str, day: str, frm: str, to: str) -> bool:
	"""Does `who` already have something overlapping [frm, to) that day?"""
	if not who:
		return False
	for other_from, other_to in store.get(who, {}).get(day, ()):  # noqa: SIM110
		if overlaps(frm, to, other_from, other_to):
			return True
	return False


def _weekday_name(date) -> str:
	names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
	return names[getdate(date).weekday()]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def generate(plan: str, variant: str | int = None, persona: str = None):
	"""Build the weekly grid for a plan, without writing it to the timetable.

	Nothing is saved: the caller gets an arrangement to look at, and the school
	timetable only changes when the grid is saved and lessons are generated.

	`variant` seeds the tie-breaking, so asking again returns a different valid
	week rather than the same one — which is what makes "build again" useful to
	someone who does not like the first attempt. Passing the same variant twice
	reproduces the same grid.
	"""
	doc = frappe.get_doc("MS Timetable Plan", plan)

	days = [d.strip() for d in (doc.working_days or "").split(",") if d.strip()]
	if not days:
		return fail(message_en="No working days set.", message_ar="لم يتم تحديد أيام الدوام.")

	teaching = [p for p in sorted(doc.periods, key=lambda x: cint(x.period_order)) if not p.is_break]
	if not teaching:
		return fail(
			message_en="The plan has no teaching periods.",
			message_ar="لا توجد حصص دراسية في الخطة.",
		)
	if not doc.subject_loads:
		return fail(message_en="No subjects in the plan.", message_ar="لم تُضف أي مواد للخطة.")

	subjects = [
		{
			"course": s.course,
			"periods_per_week": cint(s.periods_per_week) or 1,
			"instructor": s.instructor,
			"preferred_room": s.preferred_room,
			"max_per_day": cint(s.max_per_day) or 2,
		}
		for s in doc.subject_loads
	]

	teacherless = [s["course"] for s in subjects if not s.get("instructor")]

	demand = sum(s["periods_per_week"] for s in subjects)
	capacity = len(teaching) * len(days)
	if demand > capacity:
		return fail(
			message_en=f"The subjects need {demand} periods but the week only has {capacity}.",
			message_ar=f"المواد تحتاج {demand} حصة والأسبوع يتسع لـ {capacity} فقط.",
		)

	# Slots are keyed by start time, which is how existing bookings are keyed.
	slots = [_hhmmss(p.from_time) for p in teaching]
	slot_meta = {
		_hhmmss(p.from_time): {
			"name": p.period_name,
			"order": cint(p.period_order),
			"from_time": _hhmmss(p.from_time),
			"to_time": _hhmmss(p.to_time),
		}
		for p in teaching
	}

	slot_times = {s: (slot_meta[s]["from_time"], slot_meta[s]["to_time"]) for s in slots}

	busy_teacher, busy_room = _existing_bookings(doc.student_group, doc.academic_term)
	solver = Solver(
		days, slots, subjects, busy_teacher, busy_room, seed=variant, slot_times=slot_times
	)
	grid, unplaced = solver.solve()

	_assign_rooms(grid, days, slots, busy_room, slot_times)

	lessons = []
	for day in days:
		for slot in slots:
			placed = grid[day].get(slot)
			if not placed:
				continue
			meta = slot_meta[slot]
			lessons.append(
				{
					"day": day,
					"day_label": DAY_AR.get(day, day),
					"period": meta["name"],
					"period_order": meta["order"],
					"from_time": meta["from_time"],
					"to_time": meta["to_time"],
					"course": placed["course"],
					"instructor": placed.get("instructor"),
					"room": placed.get("preferred_room"),
				}
			)

	doc.status = "Generated"
	doc.generated_on = now_datetime()
	doc.generated_by = frappe.session.user
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	# Report what could not be placed, grouped, so the fix is obvious.
	shortfall: dict[str, int] = {}
	for s in unplaced:
		shortfall[s["course"]] = shortfall.get(s["course"], 0) + 1

	return {
		"plan": doc.name,
		"teacherless": teacherless,
		"days": [{"code": d, "label": DAY_AR.get(d, d)} for d in days],
		"periods": [slot_meta[s] for s in slots],
		"lessons": lessons,
		"placed": len(lessons),
		"demand": demand,
		"capacity": capacity,
		"unplaced": [{"course": c, "periods": n} for c, n in shortfall.items()],
		"complete": not shortfall,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def apply_plan(
	plan: str,
	from_date: str = None,
	weeks: int = 1,
	replace: int = 1,
	persona: str = None,
):
	"""Write the generated grid into real Course Schedule rows.

	Course Schedule is dated, not weekly, so the grid is repeated across the
	requested number of weeks starting from the given date.
	"""
	result = generate(plan=plan, persona=persona)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result
	if not data or not data.get("lessons"):
		return fail(
			message_en="Generate the timetable first.",
			message_ar="قم بتوليد الجدول أولاً.",
		)

	if data.get("teacherless"):
		names = "، ".join(data["teacherless"])
		return fail(
			message_en=f"These subjects have no teacher: {names}.",
			message_ar=f"يجب تعيين معلم لكل مادة قبل تطبيق الجدول. المواد بدون معلم: {names}",
		)

	# generate() saves the plan, so read it back rather than holding a stale copy.
	doc = frappe.get_doc("MS Timetable Plan", plan)

	start = getdate(from_date or today())
	weeks = min(max(cint(weeks) or 1, 1), 20)

	# Education refuses a lesson outside the term, so keep the window inside it
	# and say so plainly instead of failing lesson by lesson.
	term = (
		frappe.db.get_value(
			"Academic Term",
			doc.academic_term,
			["term_start_date", "term_end_date"],
			as_dict=True,
		)
		if doc.academic_term
		else None
	)
	if term and term.term_start_date and term.term_end_date:
		term_start, term_end = getdate(term.term_start_date), getdate(term.term_end_date)
		if start < term_start:
			start = term_start
		if start > term_end:
			return fail(
				message_en=f"The term ended on {term_end}. Choose a date inside it.",
				message_ar=f"الفصل الدراسي ينتهي في {term_end}. اختر تاريخاً ضمن الفصل.",
			)
		# Trim the window so it cannot run past the end of the term.
		available = (term_end - start).days + 1
		weeks = max(1, min(weeks, (available + 6) // 7))

	if cint(replace):
		# Clear the window first, so re-applying does not duplicate lessons.
		end = add_days(start, weeks * 7 - 1)
		existing = frappe.get_all(
			"Course Schedule",
			filters={
				"student_group": doc.student_group,
				"schedule_date": ["between", [str(start), str(end)]],
			},
			pluck="name",
		)
		for name in existing:
			frappe.delete_doc("Course Schedule", name, ignore_permissions=True, force=True)

	term_end = getdate(term.term_end_date) if term and term.term_end_date else None

	created, skipped = 0, []
	for week in range(weeks):
		for lesson in data["lessons"]:
			date = _next_weekday(add_days(start, week * 7), lesson["day"])
			if term_end and date > term_end:
				continue
			# Education titles a lesson "course by instructor" and crashes when
			# the teacher is missing; say which subject needs one.
			if not lesson.get("instructor"):
				skipped.append(
					{
						"date": str(date),
						"course": lesson["course"],
						"reason": "لا يوجد معلم لهذه المادة — عيّن معلماً قبل التوليد",
					}
				)
				continue

			try:
				schedule = frappe.get_doc(
					{
						"doctype": "Course Schedule",
						"student_group": doc.student_group,
						"course": lesson["course"],
						"instructor": lesson.get("instructor"),
						# "" would be rejected by the Link field; None is the
						# empty value. The room itself is optional — see the
						# make_course_schedule_room_optional patch.
						"room": lesson.get("room") or None,
						"schedule_date": date,
						"from_time": lesson["from_time"],
						"to_time": lesson["to_time"],
						"title": f"{lesson['course']} — {doc.student_group}",
					}
				)
				schedule.insert(ignore_permissions=True)
				created += 1
			except Exception as exc:
				# Education validates its own overlaps; report rather than abort.
				skipped.append(
					{
						"date": str(date),
						"course": lesson["course"],
						"reason": frappe.utils.strip_html(str(exc))[:120],
					}
				)

	doc.status = "Applied"
	doc.lessons_created = created
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"created": created, "skipped": skipped, "weeks": weeks},
		"message_en": f"Created {created} lessons.",
		"message_ar": f"تم إنشاء {created} حصة"
		+ (f" وتخطّي {len(skipped)} بسبب تعارض." if skipped else "."),
	}


def _assign_rooms(grid, days, slots, busy_room, slot_times):
	"""Give every placed lesson a room.

	Education checks a lesson's room against other lessons with the *same*
	value, and NULL matches NULL — so leaving the room empty makes every
	lesson in the grid collide with every other. Each lesson therefore gets a
	real room, preferring one that is free at that moment.
	"""
	rooms = frappe.get_all(
		"Room", fields=["name", "seating_capacity"], order_by="seating_capacity desc", limit=100
	)
	if not rooms:
		return

	for day in days:
		for slot in slots:
			lesson = grid[day].get(slot)
			if not lesson or lesson.get("preferred_room"):
				continue
			frm, to = slot_times[slot]
			free = next(
				(r.name for r in rooms if not _clashes(busy_room, r.name, day, frm, to)),
				None,
			)
			if free:
				# Copy, so the shared subject dict is not mutated for every slot.
				placed = dict(lesson)
				placed["preferred_room"] = free
				grid[day][slot] = placed
				busy_room.setdefault(free, {}).setdefault(day, []).append((frm, to))


def _next_weekday(start, day_name: str):
	"""The first date on or after `start` that falls on `day_name`."""
	target = DAY_INDEX.get(day_name, 6)
	current = getdate(start)
	for _ in range(7):
		if current.weekday() == target:
			return current
		current = getdate(add_days(current, 1))
	return getdate(start)
