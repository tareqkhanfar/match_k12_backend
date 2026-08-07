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

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, today

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

	def __init__(self, days, slots, subjects, busy_teacher, busy_room):
		self.days = days
		self.slots = slots
		self.subjects = subjects
		self.busy_teacher = busy_teacher
		self.busy_room = busy_room
		# day -> slot -> the placed lesson
		self.grid: dict[str, dict[int, dict]] = {d: {} for d in days}
		self.per_day: dict[str, dict[str, int]] = {d: {} for d in days}

	def _fits(self, subject, day, slot) -> bool:
		if slot in self.grid[day]:
			return False

		course = subject["course"]
		if self.per_day[day].get(course, 0) >= subject["max_per_day"]:
			return False

		key = (day, slot)
		teacher = subject.get("instructor")
		if teacher and key in self.busy_teacher.get(teacher, set()):
			return False
		room = subject.get("preferred_room")
		if room and key in self.busy_room.get(room, set()):
			return False
		return True

	def _place(self, subject, day, slot):
		self.grid[day][slot] = subject
		self.per_day[day][subject["course"]] = self.per_day[day].get(subject["course"], 0) + 1
		key = (day, slot)
		if subject.get("instructor"):
			self.busy_teacher.setdefault(subject["instructor"], set()).add(key)
		if subject.get("preferred_room"):
			self.busy_room.setdefault(subject["preferred_room"], set()).add(key)

	def _unplace(self, subject, day, slot):
		del self.grid[day][slot]
		self.per_day[day][subject["course"]] -= 1
		key = (day, slot)
		if subject.get("instructor"):
			self.busy_teacher.get(subject["instructor"], set()).discard(key)
		if subject.get("preferred_room"):
			self.busy_room.get(subject["preferred_room"], set()).discard(key)

	def solve(self) -> tuple[dict, list]:
		"""Return (grid, unplaced). Hardest subjects first, then backtrack."""
		# A subject pinned to a teacher and a room has the fewest options, so
		# placing it first keeps the search shallow.
		def difficulty(s):
			return (
				0 if s.get("instructor") else 1,
				0 if s.get("preferred_room") else 1,
				-s["periods_per_week"],
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
		ordered_days = sorted(
			self.days,
			key=lambda d: (
				self.per_day[d].get(subject["course"], 0),
				len(self.grid[d]),
			),
		)

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
	"""Teacher and room bookings from the rest of the school's timetable."""
	busy_teacher: dict[str, set] = {}
	busy_room: dict[str, set] = {}

	rows = frappe.get_all(
		"Course Schedule",
		filters={"student_group": ["!=", exclude_group]},
		fields=["instructor", "room", "schedule_date", "from_time"],
		limit=5000,
	)
	for r in rows:
		if not r.schedule_date:
			continue
		day = _weekday_name(r.schedule_date)
		key = (day, _hhmmss(r.from_time))
		if r.instructor:
			busy_teacher.setdefault(r.instructor, set()).add(key)
		if r.room:
			busy_room.setdefault(r.room, set()).add(key)

	# Education validates a lesson's room against Assessment Plan too, so an
	# exam booking blocks the room just as another lesson would.
	for r in frappe.get_all(
		"Assessment Plan",
		filters={"docstatus": ["<", 2]},
		fields=["room", "supervisor", "schedule_date", "from_time"],
		limit=2000,
	):
		if not r.schedule_date:
			continue
		key = (_weekday_name(r.schedule_date), _hhmmss(r.from_time))
		if r.room:
			busy_room.setdefault(r.room, set()).add(key)
		if r.supervisor:
			busy_teacher.setdefault(r.supervisor, set()).add(key)

	return busy_teacher, busy_room


def _weekday_name(date) -> str:
	names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
	return names[getdate(date).weekday()]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def generate(plan: str, persona: str = None):
	"""Build the weekly grid for a plan, without writing it to the timetable."""
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

	busy_teacher, busy_room = _existing_bookings(doc.student_group, doc.academic_term)
	solver = Solver(days, slots, subjects, busy_teacher, busy_room)
	grid, unplaced = solver.solve()

	_assign_rooms(grid, days, slots, busy_room)

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
			try:
				schedule = frappe.get_doc(
					{
						"doctype": "Course Schedule",
						"student_group": doc.student_group,
						"course": lesson["course"],
						"instructor": lesson.get("instructor"),
						"room": lesson.get("room"),
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


def _assign_rooms(grid, days, slots, busy_room):
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
			key = (day, slot)
			free = next(
				(r.name for r in rooms if key not in busy_room.get(r.name, set())),
				None,
			)
			if free:
				# Copy, so the shared subject dict is not mutated for every slot.
				placed = dict(lesson)
				placed["preferred_room"] = free
				grid[day][slot] = placed
				busy_room.setdefault(free, set()).add(key)


def _next_weekday(start, day_name: str):
	"""The first date on or after `start` that falls on `day_name`."""
	target = DAY_INDEX.get(day_name, 6)
	current = getdate(start)
	for _ in range(7):
		if current.weekday() == target:
			return current
		current = getdate(add_days(current, 1))
	return getdate(start)
