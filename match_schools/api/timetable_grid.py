
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The weekly timetable: a pattern, and the days that depart from it.

A school designs a repeating week — "Sunday, period 2, maths with Mr Ahmad in
room 1" — and that pattern is stored as `MS Timetable Slot`. Dated lessons
(`Course Schedule`) are generated from it for the term, which is what
attendance and the gradebook actually read.

When something happens on one day — a teacher off sick, two lessons swapped —
only that date changes, recorded as an `MS Lesson Change`. The pattern stays
as designed, so next week is unaffected and the cover is reportable
afterwards: who stood in, for whom, and how often.

Every write is checked against `api/scheduling.py` first, so a clash is
reported before anything is saved rather than thrown on the first offence.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, today

from match_schools.api import academic_context as ctx
from match_schools.api import scheduling as sched
from match_schools.api.utils import (
	apply_period,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	sync_group_instructors,
)


@frappe.request_cache
def _periods(student_group: str | None = None) -> list[dict]:
	"""The period definitions — the rows of the grid.

	Cached per request: a grid check asks for a period's times once per cell,
	and the definitions cannot change mid-request.

	`MS Timetable Period` is a child table of `MS Timetable Plan`, so reading it
	unfiltered returns every plan's rows stacked together. One period order is
	one row of the grid, so the rows are collapsed by order.

	A class's own plan is preferred when it has one: sections may run different
	days, and falling back to the globally newest plan would give one section
	another's clock. Without a class — drawing an empty grid, before anything is
	chosen — the most recently defined plan stands in.
	"""
	plan = None
	if student_group:
		plan = frappe.db.get_value(
			"MS Timetable Plan",
			{"student_group": student_group},
			"name",
			order_by="modified desc",
		)
	if not plan:
		plan = frappe.db.get_value(
			"MS Timetable Plan", {}, "name", order_by="modified desc"
		)
	filters = {"parenttype": "MS Timetable Plan"}
	if plan:
		filters["parent"] = plan

	rows = frappe.get_all(
		"MS Timetable Period",
		filters=filters,
		fields=["period_name", "period_order", "from_time", "to_time", "is_break"],
		order_by="period_order",
		limit_page_length=0,
	)

	seen: dict[int, dict] = {}
	for r in rows:
		order = cint(r.period_order)
		if order in seen:
			continue
		seen[order] = {
			"order": order,
			"name": r.period_name,
			"from": sched.hhmmss(r.from_time)[:5],
			"to": sched.hhmmss(r.to_time)[:5],
			"isBreak": bool(r.is_break),
		}
	return [seen[k] for k in sorted(seen)]


def _slot_row(r: dict) -> dict:
	return {
		"id": r.get("name"),
		"day": r.get("day"),
		"period": cint(r.get("period_order")),
		"from": sched.hhmmss(r.get("from_time"))[:5],
		"to": sched.hhmmss(r.get("to_time"))[:5],
		"course": r.get("course"),
		"instructor": r.get("instructor"),
		"instructorName": r.get("instructor_name"),
		"room": r.get("room"),
		"studentGroup": r.get("student_group"),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def grid_options(student_group: str = None, persona: str = None):
	"""Everything the builder needs to draw an empty grid.

	Passing a class narrows the course list to that class's programme. The
	whole catalogue is only useful before a class is chosen: offering all
	fourteen subjects to a first-grade section invites a timetable nobody
	teaches.
	"""
	return {
		"days": [{"value": k, "label": v} for k, v in sched.WEEKDAYS],
		"periods": _periods(student_group),
		# شُعب الفصل المختار وحدها: بناء جدولٍ لشعبةٍ من العام الماضي عملٌ
		# لا يُحفظ ولا يُعرض، والقائمة الكاملة تجعل اختيارها الخطأ الأسهل.
		"groups": frappe.get_all(
			"Student Group",
			filters=apply_period({"disabled": 0}, "Student Group"),
			fields=["name", "student_group_name", "program", "academic_year", "batch"],
			order_by="name",
			limit_page_length=0,
		),
		"instructors": frappe.get_all(
			"Instructor",
			filters={"status": "Active"},
			fields=["name", "instructor_name"],
			order_by="instructor_name",
		),
		"rooms": frappe.get_all("Room", fields=["name", "room_name"], order_by="name"),
		"courses": _courses_for_group(student_group),
		"defaultAcademicYear": get_default_academic_year(),
		"defaultAcademicTerm": get_default_academic_term(),
	}


def _courses_for_group(student_group: str | None) -> list[str]:
	"""The subjects a class actually studies.

	A Student Group may name a single course directly; otherwise its
	programme's course list applies. If neither yields anything the full
	catalogue is returned rather than an empty picker, so a school that has
	not filled in its programmes can still build a timetable.
	"""
	if not student_group:
		return frappe.get_all("Course", pluck="name", order_by="name")

	group = frappe.db.get_value(
		"Student Group", student_group, ["program", "course"], as_dict=True
	)
	if not group:
		return frappe.get_all("Course", pluck="name", order_by="name")

	if group.course:
		return [group.course]

	if group.program:
		courses = frappe.get_all(
			"Program Course",
			filters={"parent": group.program, "parenttype": "Program"},
			pluck="course",
			order_by="idx",
		)
		if courses:
			return courses

	return frappe.get_all("Course", pluck="name", order_by="name")


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_pattern(
	student_group: str = None,
	instructor: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""The weekly pattern for one class, or for one teacher.

	Both views read the same slots; only the filter differs. A teacher's grid
	is the same data seen from the other side, which is why it needs no
	separate storage.
	"""
	if not student_group and not instructor:
		return fail(
			"Choose a class or a teacher", "اختر شعبة أو معلماً لعرض الجدول"
		)

	# A teacher may look at their own grid without being given the whole school.
	if persona == ROLE_TEACHER and not student_group:
		scope = resolve_scope(persona)
		instructor = scope.get("instructor") or instructor

	filters: dict = {"active": 1}
	if student_group:
		filters["student_group"] = student_group
	if instructor:
		filters["instructor"] = instructor
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"MS Timetable Slot",
		filters=filters,
		fields=[
			"name", "day", "period_order", "from_time", "to_time",
			"course", "instructor", "room", "student_group",
		],
		order_by="day, period_order",
		limit_page_length=0,
	)

	names = {r.instructor for r in rows if r.instructor}
	instructor_names = (
		dict(
			frappe.get_all(
				"Instructor",
				filters={"name": ["in", list(names)]},
				fields=["name", "instructor_name"],
				as_list=True,
			)
		)
		if names
		else {}
	)
	for r in rows:
		r["instructor_name"] = instructor_names.get(r.instructor)

	return {
		"slots": [_slot_row(r) for r in rows],
		# What the rest of the school has already booked in these same periods.
		# Without it the builder looks empty and a teacher gets double-booked,
		# with the clash only surfacing on save.
		"busy": _busy_slots(student_group, instructor, academic_term),
		"periods": _grid_rows(student_group),
		"days": [{"value": k, "label": v} for k, v in sched.WEEKDAYS],
		"studentGroup": student_group,
		"instructor": instructor,
	}


def _busy_slots(
	student_group: str | None, instructor: str | None, academic_term: str | None
) -> list[dict]:
	"""Slots belonging to *other* groups that occupy the same day and period.

	Only the teacher and the room can actually clash — another class studying
	at the same time is normal — so a slot is reported only when it holds one
	of those, and the grid can then grey out that period for that teacher.
	"""
	filters: dict = {"active": 1}
	if academic_term:
		filters["academic_term"] = academic_term
	if student_group:
		filters["student_group"] = ["!=", student_group]
	elif instructor:
		filters["instructor"] = ["!=", instructor]
	else:
		return []

	rows = frappe.get_all(
		"MS Timetable Slot",
		filters=filters,
		fields=[
			"name", "day", "period_order", "from_time", "to_time",
			"course", "instructor", "room", "student_group",
		],
		order_by="day, period_order",
		limit_page_length=0,
	)
	rows = [r for r in rows if r.instructor or r.room]

	busy = [
		{
			"day": r.day,
			"periodOrder": cint(r.period_order),
			"course": r.course,
			"instructor": r.instructor,
			"room": r.room,
			"studentGroup": r.student_group,
			"source": "pattern",
		}
		for r in rows
	]

	# Lessons already generated onto the calendar count just as much as saved
	# patterns. A school that ran "generate" has its real timetable in Course
	# Schedule, and a builder that ignored it would happily double-book every
	# teacher in the school.
	busy.extend(_busy_from_lessons(student_group, instructor))
	if not busy:
		return []

	names = {b["instructor"] for b in busy if b.get("instructor")}
	instructor_names = (
		dict(
			frappe.get_all(
				"Instructor",
				filters={"name": ["in", list(names)]},
				fields=["name", "instructor_name"],
				as_list=True,
			)
		)
		if names
		else {}
	)
	for b in busy:
		b["instructorName"] = instructor_names.get(b.get("instructor"))

	return busy


def _busy_from_lessons(student_group: str | None, instructor: str | None) -> list[dict]:
	"""Generated lessons, folded back onto the weekly grid.

	A dated lesson is mapped to its weekday and matched to a period by start
	time, so a Sunday 08:00 lesson occupies period 1 of Sunday in the builder.
	"""
	periods = _periods(student_group)
	if not periods:
		return []

	# Start time -> period order, so a lesson can be placed without trusting
	# any period field on Course Schedule (it has none). `_periods()` already
	# formats its times as "HH:MM", so lesson times are trimmed to match.
	by_start = {p["from"]: cint(p["order"]) for p in periods}

	filters: dict = {"docstatus": ["<", 2]}
	if student_group:
		filters["student_group"] = ["!=", student_group]
	elif instructor:
		filters["instructor"] = ["!=", instructor]
	else:
		return []

	rows = frappe.get_all(
		"Course Schedule",
		filters=filters,
		fields=[
			"name", "schedule_date", "from_time", "to_time",
			"course", "instructor", "room", "student_group",
		],
		limit_page_length=0,
	)

	seen: set[tuple] = set()
	out: list[dict] = []
	for r in rows:
		if not (r.instructor or r.room) or not r.schedule_date:
			continue
		order = by_start.get(sched.hhmmss(r.from_time)[:5])
		if not order:
			continue
		day = sched._weekday(r.schedule_date)
		if not day:
			continue
		# The same lesson repeats weekly; one entry per slot is enough to grey
		# the cell, and collapsing here keeps the payload small.
		key = (day, order, r.instructor, r.room, r.student_group)
		if key in seen:
			continue
		seen.add(key)
		out.append(
			{
				"day": day,
				"periodOrder": order,
				"course": r.course,
				"instructor": r.instructor,
				"room": r.room,
				"studentGroup": r.student_group,
				"source": "lesson",
			}
		)
	return out


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def check_slots(slots: str | list, student_group: str = None, persona: str = None):
	"""Report conflicts for a proposed grid without saving anything.

	Called while dragging, so a cell can be marked invalid before it is
	dropped.
	"""
	proposed = parse_json_arg(slots) or []
	lessons = [
		{
			"day": s.get("day"),
			"from_time": _period_time(s, "from"),
			"to_time": _period_time(s, "to"),
			"instructor": s.get("instructor"),
			"room": s.get("room"),
			"student_group": s.get("studentGroup") or student_group,
		}
		for s in proposed
	]

	conflicts = sched.find_conflicts(
		lessons,
		exclude=_existing_slot_lessons(student_group),
		academic_term=_term_of(student_group),
	)
	return {
		"conflicts": {str(k): v for k, v in conflicts.items()},
		"summary": sched.summarise(conflicts),
	}


def _term_of(student_group: str | None) -> str | None:
	"""The term a grid belongs to — the section's own, else the current one.

	Conflict checks must be scoped to it. Without a term the check compares a
	new year's timetable against every lesson ever saved, so opening a fresh
	academic year reports the whole of last year as clashes: the same teachers
	and rooms are of course still busy in the old calendar, and nothing the
	user does to this year's grid can clear them.
	"""
	if student_group:
		term = frappe.db.get_value("Student Group", student_group, "academic_term")
		if term:
			return term
	return get_default_academic_term()


def _period_time(slot: dict, edge: str, student_group: str | None = None) -> str:
	"""A slot's time, taken from its period definition.

	The period is authoritative: a lesson in period 5 runs when period 5 runs.
	Trusting a time sent alongside the slot let the two drift apart — dragging
	a lesson between periods changed its number but kept its old clock, and the
	stored week then disagreed with the grid that produced it.

	A time supplied for a period the school has not defined is still honoured,
	so a one-off slot outside the standard day is not silently blanked.
	"""
	period = cint(slot.get("period"))
	# Which class this slot belongs to. The teacher builder and the import send
	# it inside the slot as `studentGroup`, the class builder sends one section
	# for the whole grid; reading only one of those spellings silently fell
	# back to the school-wide clock, so every stage was shown period 4 at the
	# same time even though the break moves it.
	group = slot.get("studentGroup") or slot.get("student_group") or student_group
	# This class's own clock: two stages break at different times, so the same
	# period number is not the same hour in both.
	for p in _clock(group):
		if p["order"] == period:
			return sched.hhmmss((p["from"] if edge == "from" else p["to"]) + ":00")

	direct = slot.get(edge) or slot.get(f"{edge}_time")
	if direct:
		return sched.hhmmss(direct if len(str(direct)) > 5 else f"{direct}:00")
	return ""


def _existing_slot_lessons(student_group: str | None) -> set[str]:
	"""Dated lessons generated from this group's pattern.

	Excluded when checking a redesign of that same group's week: they are the
	lessons about to be replaced, so counting them would report the grid as
	clashing with itself.
	"""
	if not student_group:
		return set()
	return set(
		frappe.get_all(
			"Course Schedule",
			filters={"student_group": student_group, "docstatus": ["<", 2]},
			pluck="name",
		)
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_pattern(
	student_group: str,
	slots: str | list,
	academic_year: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Replace one class's weekly pattern.

	Refuses outright when the grid conflicts: a timetable that double-books a
	teacher is not a draft to be fixed later, it is wrong now.
	"""
	if not frappe.db.exists("Student Group", student_group):
		return fail("Student group not found", "لم يتم العثور على الشعبة")

	proposed = parse_json_arg(slots) or []
	academic_year = academic_year or get_default_academic_year()
	academic_term = academic_term or get_default_academic_term()

	lessons = [
		{
			"day": s.get("day"),
			"from_time": _period_time(s, "from", student_group),
			"to_time": _period_time(s, "to", student_group),
			"instructor": s.get("instructor"),
			"room": s.get("room"),
			"student_group": student_group,
		}
		for s in proposed
	]

	conflicts = sched.find_conflicts(
		lessons,
		exclude=_existing_slot_lessons(student_group),
		academic_term=academic_term or _term_of(student_group),
	)
	if conflicts:
		summary = sched.summarise(conflicts)
		return fail(
			"The timetable has {0} conflict(s)".format(summary["total"]),
			"الجدول يحتوي على {0} تعارضاً — عالجها قبل الحفظ".format(summary["total"]),
			data={"conflicts": {str(k): v for k, v in conflicts.items()}, "summary": summary},
		)

	# Replace wholesale: the grid the user is looking at is the intended week.
	for existing in frappe.get_all(
		"MS Timetable Slot", filters={"student_group": student_group}, pluck="name"
	):
		frappe.delete_doc("MS Timetable Slot", existing, ignore_permissions=True, force=True)

	created = []
	for s in proposed:
		if not s.get("course"):
			continue
		doc = frappe.new_doc("MS Timetable Slot")
		doc.student_group = student_group
		doc.day = s.get("day")
		doc.period_order = cint(s.get("period"))
		doc.from_time = _period_time(s, "from", student_group)
		doc.to_time = _period_time(s, "to", student_group)
		doc.course = s.get("course")
		doc.instructor = s.get("instructor")
		doc.room = s.get("room") or None
		doc.academic_year = academic_year
		doc.academic_term = academic_term
		doc.active = 1
		doc.insert(ignore_permissions=True)
		created.append(doc.name)

	# نمط الأسبوع هو أول موضع يُسمّى فيه معلّم الشعبة، فنسجّله فوراً — قبل
	# توليد الحصص بوقت طويل، وقد لا يُولَّد أصلاً.
	sync_group_instructors(student_group)

	frappe.db.commit()
	return {
		"studentGroup": student_group,
		"slots": len(created),
		# Lessons already generated follow the saved week from today on.
		"lessons": resync_lessons({"student_group": student_group}),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def generate_lessons(
	student_group: str,
	from_date: str = None,
	to_date: str = None,
	replace: int = 1,
	audience: str = "draft",
	persona: str = None,
):
	"""Turn the weekly pattern into dated lessons for a date range.

	This is what makes the pattern real: attendance and the gradebook read
	`Course Schedule`, not the pattern.
	"""
	if not frappe.db.exists("Student Group", student_group):
		return fail("Student group not found", "لم يتم العثور على الشعبة")

	slots = frappe.get_all(
		"MS Timetable Slot",
		filters={"student_group": student_group, "active": 1},
		fields=[
			"name", "day", "period_order", "from_time", "to_time",
			"course", "instructor", "room",
		],
		limit_page_length=0,
	)
	if not slots:
		return fail(
			"This class has no weekly pattern yet",
			"لا يوجد جدول أسبوعي لهذه الشعبة — ابنِ الجدول أولاً",
		)

	start, end = _resolve_range(student_group, from_date, to_date)
	if not start or not end:
		return fail(
			"Could not determine the term dates",
			"تعذّر تحديد تواريخ الفصل — حدّد الفترة يدوياً",
		)
	if getdate(end) < getdate(start):
		return fail("The end date is before the start", "تاريخ النهاية قبل تاريخ البداية")

	result = regenerate_lessons(
		[dict(sl, student_group=student_group) for sl in slots],
		{"student_group": student_group},
		start,
		end,
		audience,
		replace=bool(cint(replace)),
	)
	_remember_until({"student_group": student_group}, end)
	_remember_audience({"student_group": student_group}, audience)

	# الجدول قال من يدرّس هذه الشعبة — نسجّله عليها، وإلا بقي «شعبي»
	# و«طلابي» فارغَين عند من ارتبط بشعبته عبر الجدول وحده.
	sync_group_instructors(student_group)

	frappe.db.commit()
	result["studentGroup"] = student_group
	return result


AUDIENCES = {
	"draft": "الإدارة فقط",
	"teachers": "المعلمون",
	"all": "المعلمون والطلاب وأولياء الأمور",
}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def publication_status(student_group: str = None, persona: str = None):
	"""How much of this class's timetable each audience can currently see."""
	if not student_group:
		return fail("Choose a class", "اختر الشعبة")

	counts: dict[str, int] = {}
	for value in AUDIENCES:
		filters = {"student_group": student_group, "docstatus": ["<", 2]}
		# Rows predating the audience field read as draft.
		filters["ms_audience"] = ["in", [value, ""]] if value == "draft" else value
		n = frappe.db.count("Course Schedule", filters)
		if n:
			counts[value] = n
	return {
		"studentGroup": student_group,
		"counts": counts,
		"total": sum(counts.values()),
		"audiences": [{"value": k, "label": v} for k, v in AUDIENCES.items()],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def publish_timetable(
	student_group: str = None,
	audience: str = "all",
	from_date: str = None,
	to_date: str = None,
	persona: str = None,
):
	"""Decide who may see a class's generated lessons.

	Separate from generating them: a week is built, checked, and only then
	released — first to the teachers who have to teach it, then to families.
	Moving back to `draft` withdraws it again.
	"""
	if not student_group:
		return fail("Choose a class", "اختر الشعبة")
	if audience not in AUDIENCES:
		return fail("Unknown audience", "جمهور غير معروف")

	filters: dict = {"student_group": student_group, "docstatus": ["<", 2]}
	if from_date and to_date:
		filters["schedule_date"] = ["between", [from_date, to_date]]

	names = frappe.get_all("Course Schedule", filters=filters, pluck="name")
	if not names:
		return fail(
			"No lessons to publish",
			"لا توجد حصص لنشرها — ولّد حصص الفصل أولاً.",
		)

	# Batched for the same reason the deletes are: a term is well over a
	# thousand lessons, and one long transaction hits the lock wait timeout.
	stamp = now_datetime() if audience != "draft" else None
	BATCH = 500
	for i in range(0, len(names), BATCH):
		chunk = names[i : i + BATCH]
		frappe.db.set_value(
			"Course Schedule",
			{"name": ["in", chunk]},
			{"ms_audience": audience, "ms_published_on": stamp},
			update_modified=False,
		)
		frappe.db.commit()

	return {
		"studentGroup": student_group,
		"audience": audience,
		"audienceLabel": AUDIENCES[audience],
		"lessons": len(names),
		"message_en": f"{len(names)} lessons are now visible to: {audience}.",
		"message_ar": f"تم ضبط ظهور {len(names)} حصة لـ: {AUDIENCES[audience]}.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def default_range(student_group: str = None, persona: str = None):
	"""The dates generation would use if none were given.

	The dialog prefills its two date fields from this, so a user sees the term
	about to be generated instead of two empty boxes and a silent assumption.
	"""
	if not student_group:
		return fail("Choose a class", "اختر الشعبة")

	start, end = _resolve_range(student_group, None, None)
	group = frappe.db.get_value(
		"Student Group", student_group, ["academic_year", "academic_term"], as_dict=True
	) or {}

	label = group.get("academic_term") or group.get("academic_year") or ""
	if not group.get("academic_term") and start:
		# The section names no term, so say which one the dates came from.
		term = frappe.db.get_value(
			"Academic Term",
			{"term_start_date": start, "term_end_date": end},
			"name",
		)
		label = term or label

	return {
		"from": str(start or ""),
		"to": str(end or ""),
		"label": label,
	}


def _resolve_range(student_group: str, from_date: str | None, to_date: str | None):
	"""The date range to generate over, defaulting to the group's term."""
	if from_date and to_date:
		return from_date, to_date

	group = frappe.db.get_value(
		"Student Group", student_group, ["academic_year", "academic_term"], as_dict=True
	)
	if group and group.academic_term:
		term = frappe.db.get_value(
			"Academic Term",
			group.academic_term,
			["term_start_date", "term_end_date"],
			as_dict=True,
		)
		if term and term.term_start_date:
			return from_date or term.term_start_date, to_date or term.term_end_date

	# A section with no term of its own falls back to the school's current term
	# rather than the whole academic year. Generating a year at once is 1400+
	# lessons and took ~50 seconds — long enough for the gateway to cut the
	# request off, which reached the browser as an unparseable response rather
	# than a result. A term is the unit a school actually timetables anyway.
	if group and group.academic_year:
		term = frappe.db.get_value(
			"Academic Term",
			{
				"academic_year": group.academic_year,
				"term_start_date": ["<=", today()],
				"term_end_date": [">=", today()],
			},
			["term_start_date", "term_end_date"],
			as_dict=True,
		) or frappe.db.get_value(
			"Academic Term",
			{"academic_year": group.academic_year},
			["term_start_date", "term_end_date"],
			as_dict=True,
			order_by="term_start_date",
		)
		if term and term.term_start_date:
			return from_date or term.term_start_date, to_date or term.term_end_date

		year = frappe.db.get_value(
			"Academic Year",
			group.academic_year,
			["year_start_date", "year_end_date"],
			as_dict=True,
		)
		if year and year.year_start_date:
			return from_date or year.year_start_date, to_date or year.year_end_date

	return from_date, to_date


DEFAULT_DAILY_LIMIT = 2


def _limit_key(group: str, course: str) -> str:
	return f"ms_daily_limit:{group}:{course}"


def _daily_limit(group: str, course: str, plan_value=None) -> int:
	"""How many lessons of one subject a section may have in a day.

	Set per section and subject, because the answer differs: a double period
	of maths is normal where two periods of art are not. What the timetabler
	set on the screen wins over the plan, and two is the fallback.
	"""
	stored = frappe.db.get_default(_limit_key(group, course))
	if stored:
		return cint(stored)
	return cint(plan_value) or DEFAULT_DAILY_LIMIT


def _save_daily_limits(limits: dict) -> None:
	for key, value in (limits or {}).items():
		group, _, course = str(key).partition("#")
		if group and course and cint(value) > 0:
			frappe.db.set_default(_limit_key(group, course), cint(value))


def _group_labels(names: set[str]) -> dict:
	if not names:
		return {}
	return {
		r.name: r.student_group_name or r.name
		for r in frappe.get_all(
			"Student Group",
			filters={"name": ["in", list(names)]},
			fields=["name", "student_group_name"],
		)
	}


def school_grid() -> tuple[list[dict], list[str]]:
	"""The school-wide week: its periods and its working days.

	A school that has not built a single class plan has no period rows yet, and
	a grid would draw no rows at all. The school day is defined independently
	of any class, so it stands in.
	"""
	from match_schools.api.timetable import school_day_shape

	shape = school_day_shape()
	return _periods(None) or _slot_clock(None) or _shape_clock(), shape["working_days"]


def _slot_clock(student_group: str | None) -> list[dict]:
	"""The periods a timetable already in use runs on, read from its slots.

	A school that imported its week has no plan, but every slot carries its
	period number and times — that is the clock. The most common time per
	period wins, since sections may run a period minutes apart.
	"""
	filters = {"active": 1}
	if student_group:
		filters["student_group"] = student_group
	tally: dict[int, dict] = {}
	for r in frappe.get_all(
		"MS Timetable Slot",
		filters=filters,
		fields=["period_order", "from_time", "to_time"],
		limit_page_length=0,
	):
		key = (sched.hhmmss(r.from_time)[:5], sched.hhmmss(r.to_time)[:5])
		counts = tally.setdefault(cint(r.period_order), {})
		counts[key] = counts.get(key, 0) + 1
	return [
		{
			"order": order,
			"name": f"الحصة {order}",
			"from": max(counts, key=counts.get)[0],
			"to": max(counts, key=counts.get)[1],
			"isBreak": False,
		}
		for order, counts in sorted(tally.items())
		if order
	]


def _shape_clock() -> list[dict]:
	"""The school day as configured, teaching periods numbered 1..n.

	Breaks are left out and the lessons numbered consecutively, the way a
	school counts them — so "period 3" is the third lesson whether or not a
	break comes before it.
	"""
	from match_schools.api.timetable import build_periods, school_day_shape

	shape = school_day_shape()
	teaching = [
		p
		for p in build_periods(
			count=shape["count"],
			minutes=shape["minutes"],
			start=shape["start"],
			gap=shape["gap"],
			break_after=shape["break_after"],
			break_minutes=shape["break_minutes"],
		)
		if not p["is_break"]
	]
	return [
		{
			"order": i + 1,
			"name": p["period_name"],
			"from": sched.hhmmss(p["from_time"])[:5],
			"to": sched.hhmmss(p["to_time"])[:5],
			"isBreak": False,
		}
		for i, p in enumerate(teaching)
	]


@frappe.request_cache
def _clocks_by_level() -> dict:
	"""The clock each grade level actually runs, read from the timetable.

	A school may run two bells — the younger grades break before the fourth
	lesson and the older ones after it — and the difference is real: the same
	"period 4" is a different time for each. Rather than hard-code one school's
	bells, the times in use are read back from the saved week, per level.
	"""
	levels = {
		g.name: cint(level)
		for g in frappe.get_all(
			"Student Group", fields=["name", "program"], limit_page_length=0
		)
		for level in [frappe.db.get_value("Program", g.program, "ms_level") if g.program else 0]
	}
	tally: dict[int, dict[int, dict]] = {}
	for r in frappe.get_all(
		"MS Timetable Slot",
		filters={"active": 1},
		fields=["student_group", "period_order", "from_time", "to_time"],
		limit_page_length=0,
	):
		level = levels.get(r.student_group)
		if level is None:
			continue
		times = (sched.hhmmss(r.from_time)[:5], sched.hhmmss(r.to_time)[:5])
		counts = tally.setdefault(level, {}).setdefault(cint(r.period_order), {})
		counts[times] = counts.get(times, 0) + 1
	return {
		level: [
			{
				"order": order,
				"name": str(order),
				"from": max(counts, key=counts.get)[0],
				"to": max(counts, key=counts.get)[1],
				"isBreak": False,
			}
			for order, counts in sorted(periods.items())
			if order
		]
		for level, periods in tally.items()
	}


def _level_of(student_group: str | None) -> int | None:
	if not student_group:
		return None
	program = frappe.db.get_value("Student Group", student_group, "program")
	if not program:
		return None
	return cint(frappe.db.get_value("Program", program, "ms_level"))


def _peer_clock(student_group: str | None) -> list[dict]:
	"""The clock of the nearest grade level that has a timetable.

	A section with no lessons of its own — a new one, or a year that has not
	been timetabled yet — still has a real bell: the one its own grade runs,
	or failing that the grade closest to it. Nearest by level rather than by
	name, so nothing depends on how a school spells "الصف الأول".
	"""
	level = _level_of(student_group)
	if level is None:
		return []
	clocks = _clocks_by_level()
	if not clocks:
		return []
	if level in clocks:
		return clocks[level]
	# Ties go to the lower grade: a kindergarten section follows grade one.
	nearest = min(clocks, key=lambda other: (abs(other - level), other))
	return clocks[nearest]


@frappe.request_cache
def _school_clock() -> list[dict]:
	"""The school day as lessons only, keeping the school's own numbering.

	The school-wide grid may carry its break as a row of its own, and a break
	is not a period: offered as one it became "الحصة 4" on screen, twenty
	minutes long, with no lesson ever in it. Its number is left out rather
	than closed up, because a school whose break is period 4 numbers the
	lesson after it 5 — and that is the number in its saved week.
	"""
	return [p for p in school_grid()[0] if not p.get("isBreak")]


def clocks_for(groups: list[str]) -> dict:
	"""Each of these classes' clocks, in one pass.

	The teacher builder draws one grid across the whole school, so every class
	on it needs its own times — asking `_clock` per class would be a query per
	class. The per-level clocks and the school day are read once and shared;
	only the classes' own saved slots are fetched, in a single query.
	"""
	if not groups:
		return {}
	own: dict[str, dict[int, dict]] = {}
	for r in frappe.get_all(
		"MS Timetable Slot",
		filters={"active": 1, "student_group": ["in", groups]},
		fields=["student_group", "period_order", "from_time", "to_time"],
		limit_page_length=0,
	):
		order = cint(r.period_order)
		if not order:
			continue
		own.setdefault(r.student_group, {}).setdefault(
			order,
			{
				"order": order,
				"name": str(order),
				"from": sched.hhmmss(r.from_time)[:5],
				"to": sched.hhmmss(r.to_time)[:5],
				"isBreak": False,
			},
		)
	shape = _school_clock()
	out = {}
	for group in groups:
		periods = dict(own.get(group) or {})
		for source in (_peer_clock(group), shape):
			for p in source or []:
				periods.setdefault(cint(p["order"]), p)
		out[group] = [periods[o] for o in sorted(periods)]
	return out


def _grid_rows(student_group: str | None) -> list[dict]:
	"""The rows of one class's builder: the periods it really runs.

	Its own clock decides, so the time on a row is the time the lesson is
	saved with — a plan written before the week was built could say 09:55 for
	a period the class has run at 10:10 all year. A break the plan defines is
	kept, unless a lesson already uses that number, in which case the lesson
	wins: a period nobody can fill is worse than a break nobody can see.
	"""
	rows = {cint(p["order"]): dict(p) for p in _clock(student_group)}
	for p in _periods(student_group):
		if p.get("isBreak") and cint(p["order"]) not in rows:
			rows[cint(p["order"])] = dict(p)
	for order, row in rows.items():
		if not row.get("name") or str(row["name"]).strip().isdigit():
			row["name"] = f"الحصة {order}"
	return [rows[o] for o in sorted(rows)]


def _clock(student_group: str | None) -> list[dict]:
	"""One class's periods: its plan, else its own slots, else the school's.

	Everything that turns a period number into a time goes through here, so a
	school with no plan at all still gets real times rather than blanks.

	A class's own slots only cover the periods it already uses, so the school's
	clock fills the rest: without that, putting a lesson in a period the class
	has never used produced a slot with no time, and the clash check then broke
	on an empty string rather than saying anything useful.
	"""
	# The saved week is the truth: what a period actually runs at is what the
	# slots say, not what a plan written earlier assumed. The plan, the grade's
	# clock and the school day fill in periods the section has never used — a
	# section with no lesson in period 1 all week still has a period 1.
	own = _slot_clock(student_group)
	known = {cint(p["order"]) for p in own}
	# Then the classes of the same grade: their saved slots are real times for
	# the same school day, so they beat this section's plan, which is a template
	# someone typed once and may never have matched the week that was built.
	# This section's own plan counts; another section's does not. `_periods`
	# falls back to the newest plan in the school, which says nothing about
	# this class — its grade's clock is the better answer.
	own_plan = (
		_periods(student_group)
		if student_group
		and frappe.db.exists("MS Timetable Plan", {"student_group": student_group})
		else []
	)
	for source in (_peer_clock(student_group), own_plan, _school_clock()):
		for p in source or []:
			# A break is not a period: a class has no lesson in it, and a row
			# for it on a timetable is a lie about the school day.
			if p.get("isBreak") or cint(p["order"]) in known:
				continue
			known.add(cint(p["order"]))
			own.append(p)
	return sorted(own, key=lambda p: cint(p["order"]))


def grid_periods() -> tuple[list[dict], list[str]]:
	"""The rows of a grid that spans the whole school, and its working days.

	The periods the timetable actually uses, numbered as it numbers them, so
	the number on a row is the number that gets stored. The school-wide grid
	may come from a plan that counts its break as a period, and taking the
	rows from there renumbered the lessons around it: the row labelled 3 saved
	period 4, and the screen and the register then disagreed.
	"""
	working_days = school_grid()[1]
	tally: dict[int, dict[tuple, int]] = {}
	for clock in _clocks_by_level().values():
		for p in clock:
			counts = tally.setdefault(cint(p["order"]), {})
			key = (p["from"], p["to"])
			counts[key] = counts.get(key, 0) + 1
	shape = {cint(p["order"]): p for p in _school_clock()}
	real = {
		order: {
			"order": order,
			"name": f"الحصة {order}",
			"from": max(counts, key=counts.get)[0],
			"to": max(counts, key=counts.get)[1],
			"isBreak": False,
		}
		for order, counts in tally.items()
	}
	# Every period either of them knows about: the timetable cannot have fewer
	# periods than it already uses, and a school part-way through building its
	# first week still needs the rest of the day to fill in — with two lessons
	# saved, a grid of two rows has nowhere to put the rest. Only periods one
	# of them names, never a number invented to close a gap: a school whose
	# break is period 4 has no period 4 to offer.
	return [
		real.get(order) or shape[order]
		for order in sorted(set(real) | set(shape))
	], working_days


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def teacher_grid_options(persona: str = None):
	"""The teacher builder's pickers: every class with the subjects it studies.

	A school entering a paper timetable works down one teacher's row, so it
	needs every class at once rather than one class's courses at a time.
	"""
	groups = frappe.get_all(
		"Student Group",
		filters=apply_period({"disabled": 0}, "Student Group"),
		fields=["name", "student_group_name", "program", "academic_year", "batch"],
		order_by="program, student_group_name",
		limit_page_length=0,
	)
	for g in groups:
		g["courses"] = _courses_for_group(g["name"])

	periods, working_days = grid_periods()

	assigned = {}
	for r in frappe.get_all(
		"MS Timetable Slot", filters={"active": 1}, fields=["instructor"], limit_page_length=0
	):
		if r.instructor:
			assigned[r.instructor] = assigned.get(r.instructor, 0) + 1

	# Each class's own times, so the grid can say what a cell really is: the
	# same period number is a different hour for the younger and older grades,
	# and one time at the head of the row was wrong for half the school.
	clocks = clocks_for([g["name"] for g in groups])
	for g in groups:
		g["clock"] = clocks.get(g["name"], [])


	return {
		"days": [{"value": k, "label": v} for k, v in sched.WEEKDAYS],
		"workingDays": working_days,
		"periods": periods,
		"groups": groups,
		"instructors": [
			{
				"name": i.name,
				"instructor_name": i.instructor_name,
				"quota": cint(i.get("ms_weekly_quota")),
				"assigned": assigned.get(i.name, 0),
			}
			for i in frappe.get_all(
				"Instructor",
				filters={"status": "Active"},
				fields=["name", "instructor_name", "ms_weekly_quota"],
				order_by="instructor_name",
			)
		],
		"rooms": frappe.get_all("Room", fields=["name", "room_name"], order_by="name"),
		"defaultAcademicYear": get_default_academic_year(),
		"defaultAcademicTerm": get_default_academic_term(),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def taken_periods(instructor: str = None, persona: str = None):
	"""Which classes are already booked, and by whom, in every period.

	The teacher builder needs this to grey out a cell before it is clicked: a
	section cannot sit in two lessons at once, however free the teacher is.
	`instructor`'s own slots are excluded — those are the ones being redrawn.
	"""
	rows = frappe.get_all(
		"MS Timetable Slot",
		filters={"active": 1},
		fields=["day", "period_order", "student_group", "instructor", "course", "room"],
		limit_page_length=0,
	)
	taken = [
		{
			"day": r.day,
			"period": cint(r.period_order),
			"studentGroup": r.student_group,
			"instructor": r.instructor,
			"course": r.course,
			"room": r.room,
		}
		for r in rows
		if not instructor or r.instructor != instructor
	]

	# Lessons already generated for the term occupy the week too, and the save
	# is checked against them. Leaving them out would let a cell be filled on
	# screen only to be refused on save — the grid must show what the server
	# will accept. Each lesson is matched against its own class's clock:
	# sections may run different period times, and one school-wide mapping
	# silently dropped the sections that differ.
	starts: dict[str, dict] = {}
	for r in frappe.get_all(
		"Course Schedule",
		filters={"docstatus": ["<", 2]},
		fields=["schedule_date", "from_time", "to_time", "course", "instructor", "room", "student_group"],
		limit_page_length=0,
	):
		if not r.schedule_date or not r.student_group:
			continue
		if instructor and r.instructor == instructor:
			continue
		if r.student_group not in starts:
			starts[r.student_group] = _clock(r.student_group)
		day = sched._weekday(r.schedule_date)
		if not day:
			continue
		# Matched by overlap rather than by an identical start: lessons
		# generated from an older bell schedule run minutes apart from the
		# current one, and an exact-start match reported those cells free —
		# the save then refused them.
		lesson_from = sched.hhmmss(r.from_time)
		lesson_to = sched.hhmmss(r.to_time or r.from_time)
		for period in starts[r.student_group]:
			if sched.overlaps(
				lesson_from, lesson_to, f"{period['from']}:00", f"{period['to']}:00"
			):
				taken.append(
					{
						"day": day,
						"period": cint(period["order"]),
						"studentGroup": r.student_group,
						"instructor": r.instructor,
						"course": r.course,
						"room": r.room,
						"from": lesson_from[:5],
						"to": lesson_to[:5],
					}
				)

	labels = _group_labels({t["studentGroup"] for t in taken if t["studentGroup"]})
	names = dict(
		frappe.get_all(
			"Instructor",
			filters={"status": ["!=", ""]},
			fields=["name", "instructor_name"],
			as_list=True,
		)
	)
	seen = set()
	unique = []
	for t in taken:
		key = (t["day"], t["period"], t["studentGroup"])
		if key in seen:
			continue
		seen.add(key)
		t["studentGroupName"] = labels.get(t["studentGroup"], t["studentGroup"])
		t["instructorName"] = names.get(t["instructor"], t["instructor"])
		unique.append(t)
	return {"taken": unique}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def teacher_assignments(instructor: str, persona: str = None):
	"""What this teacher owes each class per week, and what is already placed.

	The class plans already record "this section studies maths four periods a
	week with this teacher". Reading them here means the teacher screen counts
	against the same figures the class screen was built from, instead of asking
	the timetabler to retype them.
	"""
	# A class may carry several plans from earlier attempts; the newest is the
	# one the class builder reads, so the others must not be counted twice.
	plans = {}
	current_plan = {}
	for p in frappe.get_all(
		"MS Timetable Plan",
		fields=["name", "student_group"],
		order_by="modified desc",
		limit_page_length=0,
	):
		plans[p.name] = p
		current_plan.setdefault(p.student_group, p.name)

	rows = [
		r
		for r in frappe.get_all(
			"MS Subject Load",
			filters={"instructor": instructor, "parenttype": "MS Timetable Plan"},
			fields=["parent", "course", "periods_per_week", "max_per_day", "preferred_room"],
			limit_page_length=0,
		)
		if current_plan.get((plans.get(r.parent) or {}).get("student_group")) == r.parent
	]
	placed: dict[tuple, int] = {}
	for r in frappe.get_all(
		"MS Timetable Slot",
		filters={"instructor": instructor, "active": 1},
		fields=["student_group", "course"],
		limit_page_length=0,
	):
		placed[(r.student_group, r.course)] = placed.get((r.student_group, r.course), 0) + 1

	out = []
	seen = set()
	for r in rows:
		plan = plans.get(r.parent)
		if not plan or not plan.student_group:
			continue
		key = (plan.student_group, r.course)
		if key in seen:
			continue
		seen.add(key)
		out.append({
			"studentGroup": plan.student_group,
			"course": r.course,
			"required": cint(r.periods_per_week),
			"maxPerDay": _daily_limit(plan.student_group, r.course, r.max_per_day),
			"room": r.preferred_room,
			"placed": placed.get(key, 0),
		})

	# Lessons already on the grid that no plan mentions: a week entered here
	# first, or a plan since edited. Listing them keeps the totals honest.
	for (group, course), count in placed.items():
		if (group, course) not in seen and group and course:
			out.append({
				"studentGroup": group,
				"course": course,
				"required": count,
				"maxPerDay": _daily_limit(group, course),
				"room": None,
				"placed": count,
				"fromGrid": True,
			})

	labels = _group_labels({a["studentGroup"] for a in out})
	for a in out:
		a["studentGroupName"] = labels.get(a["studentGroup"], a["studentGroup"])

	quota = cint(frappe.db.get_value("Instructor", instructor, "ms_weekly_quota"))
	return {
		"assignments": sorted(out, key=lambda a: (a["studentGroupName"], a["course"])),
		"quota": quota,
		"placed": sum(placed.values()),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def check_teacher_slots(instructor: str, slots: str | list = None, persona: str = None):
	"""Validate a teacher's proposed week without saving it.

	The screen calls this before the save button lights up, so a clash is
	pointed at on the grid rather than refused after the fact. It runs the very
	checks the save runs — a cell that passes here saves.
	"""
	proposed = [
		s for s in (parse_json_arg(slots) or []) if s.get("course") and s.get("studentGroup")
	]
	for s in proposed:
		s["student_group"] = s.get("studentGroup")

	problems = _teacher_clashes(instructor, proposed)
	timeless = _timeless(proposed)
	if timeless:
		return {"problems": problems + [{"day": None, "period": None, "studentGroup": None, "message": timeless}], "quota": 0, "placed": len(proposed), "overQuota": False}
	lessons = [
		{
			"day": s.get("day"),
			"from_time": _period_time(s, "from"),
			"to_time": _period_time(s, "to"),
			"instructor": instructor,
			"room": s.get("room"),
			"student_group": s.get("studentGroup"),
		}
		for s in proposed
	]
	exclude = set(
		frappe.get_all(
			"Course Schedule",
			filters={"instructor": instructor, "docstatus": ["<", 2]},
			pluck="name",
		)
	)
	conflicts = sched.find_conflicts(lessons, exclude=exclude, academic_term=None)
	for index, items in conflicts.items():
		s = proposed[index] if index < len(proposed) else {}
		problems.append(
			{
				"day": s.get("day"),
				"period": cint(s.get("period")),
				"studentGroup": s.get("studentGroup"),
				"message": items[0]["detail"] if items else items,
			}
		)

	quota = cint(frappe.db.get_value("Instructor", instructor, "ms_weekly_quota"))
	return {
		"problems": problems,
		"quota": quota,
		"placed": len(proposed),
		"overQuota": bool(quota and len(proposed) > quota),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_teacher_pattern(
	instructor: str,
	slots: str | list,
	academic_year: str = None,
	academic_term: str = None,
	limits: str | dict = None,
	persona: str = None,
):
	"""Replace one teacher's week, across every class they teach.

	The class builder replaces a class's whole week; this replaces a teacher's,
	and the two write the same slots. Only this teacher's rows are touched, so
	a class keeps whatever its other teachers have already been given.
	"""
	if not frappe.db.exists("Instructor", instructor):
		return fail("Instructor not found", "لم يتم العثور على المعلم")

	proposed = [s for s in (parse_json_arg(slots) or []) if s.get("course") and s.get("studentGroup")]
	for s in proposed:
		# `_period_time` reads a class's own period plan under this key.
		s["student_group"] = s.get("studentGroup")
	academic_year = academic_year or get_default_academic_year()
	academic_term = academic_term or get_default_academic_term()

	# A section already promised to another teacher in that period. The dated
	# lessons below cannot catch this on a school that has not generated any.
	timeless = _timeless(proposed)
	if timeless:
		return fail("Some periods have no time defined.", timeless)

	problems = _teacher_clashes(instructor, proposed)
	if problems:
		return fail(
			"The timetable has {0} conflict(s)".format(len(problems)),
			"تعارضات: " + "، ".join(p["message"] for p in problems[:6]),
			data={"problems": problems},
		)

	# النصاب: a teacher's contract caps the week, and a timetable that exceeds
	# it is not a draft to fix later — it cannot be worked.
	quota = cint(frappe.db.get_value("Instructor", instructor, "ms_weekly_quota"))
	if quota and len(proposed) > quota:
		return fail(
			"Weekly quota exceeded: {0} of {1}".format(len(proposed), quota),
			"عدد الحصص {0} يتجاوز نصاب المعلم الأسبوعي ({1})".format(len(proposed), quota),
			data={"quota": quota, "placed": len(proposed)},
		)

	lessons = [
		{
			"day": s.get("day"),
			"from_time": _period_time(s, "from"),
			"to_time": _period_time(s, "to"),
			"instructor": instructor,
			"room": s.get("room"),
			"student_group": s.get("studentGroup"),
		}
		for s in proposed
	]
	exclude = set(
		frappe.get_all(
			"Course Schedule",
			filters={"instructor": instructor, "docstatus": ["<", 2]},
			pluck="name",
		)
	)
	conflicts = sched.find_conflicts(lessons, exclude=exclude, academic_term=academic_term)
	if conflicts:
		summary = sched.summarise(conflicts)
		return fail(
			"The timetable has {0} conflict(s)".format(summary["total"]),
			"الجدول يحتوي على {0} تعارضاً — عالجها قبل الحفظ".format(summary["total"]),
			data={"conflicts": {str(k): v for k, v in conflicts.items()}, "summary": summary},
		)

	touched = {s.get("studentGroup") for s in proposed}
	for existing in frappe.get_all(
		"MS Timetable Slot", filters={"instructor": instructor}, fields=["name", "student_group"]
	):
		touched.add(existing.student_group)
		frappe.delete_doc("MS Timetable Slot", existing.name, ignore_permissions=True, force=True)

	created = 0
	for s in proposed:
		doc = frappe.new_doc("MS Timetable Slot")
		doc.student_group = s.get("studentGroup")
		doc.day = s.get("day")
		doc.period_order = cint(s.get("period"))
		doc.from_time = _period_time(s, "from")
		doc.to_time = _period_time(s, "to")
		doc.course = s.get("course")
		doc.instructor = instructor
		doc.room = s.get("room") or None
		doc.academic_year = academic_year
		doc.academic_term = academic_term
		doc.active = 1
		doc.insert(ignore_permissions=True)
		created += 1

	for group in touched:
		if group:
			sync_group_instructors(group)

	_save_daily_limits(parse_json_arg(limits, {}) or {})
	_sync_plans(instructor, proposed, parse_json_arg(limits, {}) or {})

	frappe.db.commit()
	return {
		"instructor": instructor,
		"slots": created,
		"groups": sorted(g for g in touched if g),
		# Lessons already generated follow the saved week from today on.
		"lessons": resync_lessons({"instructor": instructor}),
	}


def _sync_plans(instructor: str, proposed: list[dict], limits: dict = None) -> None:
	"""Write what was just placed back into each class's plan.

	The class builder arranges a week from its plan — "four periods of maths
	with this teacher" — so a week entered on the teacher screen has to update
	those figures too, or the next automatic arrangement would undo it. Only
	rows for this teacher's subjects are touched; a plan without a row for the
	subject gains one, and a class with no plan at all is left to the class
	screen, which is where plans are created.
	"""
	counts: dict[tuple, int] = {}
	for s in proposed:
		key = (s.get("studentGroup"), s.get("course"))
		counts[key] = counts.get(key, 0) + 1

	for (group, course), count in counts.items():
		plan_name = frappe.db.get_value(
			"MS Timetable Plan", {"student_group": group}, "name", order_by="modified desc"
		)
		if not plan_name:
			continue
		plan = frappe.get_doc("MS Timetable Plan", plan_name)
		row = next((r for r in plan.subject_loads if r.course == course), None)
		limit = cint((limits or {}).get(f"{group}#{course}")) or _daily_limit(group, course)
		if row:
			# Another teacher may own this subject in the plan; the grid is the
			# newer decision, so it wins — for this subject only.
			row.instructor = instructor
			row.periods_per_week = count
			row.max_per_day = limit
		else:
			plan.append(
				"subject_loads",
				{
					"course": course,
					"instructor": instructor,
					"periods_per_week": count,
					"max_per_day": limit,
				},
			)
		plan.save(ignore_permissions=True)


def _teacher_clashes(instructor: str, proposed: list[dict]) -> list[dict]:
	"""Cells this teacher cannot have: a section already promised elsewhere,
	or two of their own lessons at the same time.

	Judged by the clock, not by the period number. Two sections can run the
	same numbered period at different times — where the break falls decides —
	so comparing numbers would both invent clashes that do not exist and miss
	ones that do.
	"""
	booked = {
		(r.day, cint(r.period_order), r.student_group): r
		for r in frappe.get_all(
			"MS Timetable Slot",
			filters={"active": 1, "instructor": ["!=", instructor]},
			fields=["day", "period_order", "student_group", "instructor", "course"],
			limit_page_length=0,
		)
	}
	labels = _group_labels({s.get("studentGroup") for s in proposed})
	problems: list[dict] = []
	seen: list[dict] = []
	for s in proposed:
		day, period = s.get("day"), cint(s.get("period"))
		group = s.get("studentGroup")
		group_label = labels.get(group, group)
		start, end = _period_time(s, "from"), _period_time(s, "to")
		other = booked.get((day, period, group))
		if other:
			who = (
				frappe.db.get_value("Instructor", other.instructor, "instructor_name")
				or other.instructor
			)
			problems.append({
				"day": day, "period": period, "studentGroup": group,
				"message": "{0}: محجوزة لدى {1}".format(group_label, who)
				if who
				else "{0}: الحصة محجوزة مسبقاً".format(group_label),
			})
		# The teacher's own week, compared by the clock each section runs.
		for earlier in seen:
			if earlier["day"] != day or not (start and end):
				continue
			if sched.overlaps(start, end, earlier["from"], earlier["to"]):
				problems.append({
					"day": day, "period": period, "studentGroup": group,
					"message": "{0}: تتعارض مع {1} في الوقت نفسه ({2}–{3})".format(
						group_label, earlier["label"], start[:5], end[:5]
					),
				})
				break
		seen.append({"day": day, "from": start, "to": end, "label": group_label})
	return problems


# --- Dated lessons: one engine for every screen that generates them ----------


def _protected(names: list[str]) -> set[str]:
	"""Lessons a regeneration must leave alone.

	A lesson with a substitution or swap recorded against it is a decision, and
	one with a register taken is a record of what happened; deleting either
	would lose it and orphan whatever points at it.
	"""
	if not names:
		return set()
	changed = set(
		frappe.get_all(
			"MS Lesson Change",
			filters={"docstatus": 1, "course_schedule": ["in", names]},
			pluck="course_schedule",
		)
	)
	attended = set(
		frappe.get_all(
			"Student Attendance",
			filters={"course_schedule": ["in", names]},
			pluck="course_schedule",
		)
	)
	return changed | attended


def regenerate_lessons(
	slots: list[dict],
	scope: dict,
	start,
	end,
	audience: str = "draft",
	replace: bool = True,
) -> dict:
	"""Replace the dated lessons in `scope` between two dates with `slots`.

	`scope` names whose lessons are being replaced — a teacher's or a
	section's — and nothing outside it is deleted. Protected lessons stay, and
	no new lesson is created on top of one: the kept lesson already occupies
	that section, and that teacher, at that time.
	"""
	start, end = getdate(start), getdate(end)
	existing = frappe.get_all(
		"Course Schedule",
		filters={**scope, "schedule_date": ["between", [start, end]], "docstatus": ["<", 2]},
		fields=["name", "student_group", "instructor", "schedule_date", "from_time"],
		limit_page_length=0,
	)
	keep = _protected([e.name for e in existing]) if replace else {e.name for e in existing}
	doomed = [e.name for e in existing if e.name not in keep]

	# What a kept lesson occupies, so the new week is not laid over it.
	held_group: set[tuple] = set()
	held_teacher: set[tuple] = set()
	for e in existing:
		if e.name in keep:
			at = (str(e.schedule_date), sched.hhmmss(e.from_time))
			held_group.add((e.student_group, *at))
			if e.instructor:
				held_teacher.add((e.instructor, *at))

	removed = 0
	for i in range(0, len(doomed), 200):
		chunk = doomed[i : i + 200]
		frappe.db.delete("Course Schedule", {"name": ["in", chunk]})
		removed += len(chunk)
		frappe.db.commit()

	by_day: dict[str, list] = {}
	for s in slots:
		by_day.setdefault(s["day"], []).append(s)

	created, kept_over, skipped, committed_at = 0, 0, [], 0
	current = start
	while current <= end:
		holiday = ctx.holiday_reason(current)
		if holiday:
			if by_day.get(current.strftime("%A")):
				skipped.append({"date": str(current), "course": None, "reason": holiday})
			current = add_days(current, 1)
			continue
		for s in by_day.get(current.strftime("%A"), []):
			at = (str(current), sched.hhmmss(s["from_time"]))
			if (s["student_group"], *at) in held_group or (
				s.get("instructor") and (s["instructor"], *at) in held_teacher
			):
				kept_over += 1
				continue
			if not s.get("instructor"):
				skipped.append({
					"date": str(current), "course": s["course"],
					"reason": "لا يوجد معلم لهذه المادة — عيّن معلماً قبل التوليد",
				})
				continue
			try:
				doc = frappe.new_doc("Course Schedule")
				doc.student_group = s["student_group"]
				doc.course = s["course"]
				doc.instructor = s["instructor"]
				doc.room = s.get("room") or None
				doc.schedule_date = current
				doc.from_time = s["from_time"]
				doc.to_time = s["to_time"]
				doc.ms_audience = audience
				if audience != "draft":
					doc.ms_published_on = now_datetime()
				doc.insert(ignore_permissions=True)
				created += 1
			except Exception as exc:
				frappe.clear_messages()
				skipped.append({"date": str(current), "course": s["course"], "reason": str(exc)[:160]})
		if created - committed_at >= 200:
			frappe.db.commit()
			committed_at = created
		current = add_days(current, 1)

	frappe.db.commit()
	return {
		"from": str(start),
		"to": str(end),
		"created": created,
		"removed": removed,
		"keptAttended": len(keep),
		"keptOver": kept_over,
		"skipped": skipped,
	}


def _until_key(scope: dict) -> str:
	kind, value = next(iter(scope.items()))
	return f"ms_lessons_until:{kind}:{value}"


def _remember_audience(scope: dict, audience: str) -> None:
	frappe.db.set_default(_until_key(scope).replace("until", "audience", 1), audience)


def _remember_until(scope: dict, end) -> None:
	"""Record how far lessons were generated for this teacher or section.

	The last lesson on the calendar is not the same thing: a range ending on a
	Thursday whose last lesson is Monday would leave a lesson moved onto
	Wednesday ungenerated by the next sync.
	"""
	key = _until_key(scope)
	stored = frappe.db.get_default(key)
	if not stored or getdate(end) > getdate(stored):
		frappe.db.set_default(key, str(getdate(end)))


def _generated_until(scope: dict, since):
	"""How far a sync must reach: the furthest any generation covered.

	A teacher's lessons may have been generated section by section, and a
	section's teacher by teacher, so both kinds of record count.
	"""
	dates = [_last_lesson_date(scope, since)]
	dates.append(frappe.db.get_default(_until_key(scope)))
	field, value = next(iter(scope.items()))
	other = "student_group" if field == "instructor" else "instructor"
	for related in frappe.get_all(
		"MS Timetable Slot", filters={field: value}, pluck=other, distinct=True
	):
		if related:
			dates.append(frappe.db.get_default(_until_key({other: related})))
	dates = [getdate(d) for d in dates if d]
	return max(dates) if dates else None


def _last_lesson_date(scope: dict, since):
	rows = frappe.get_all(
		"Course Schedule",
		filters={**scope, "schedule_date": [">=", since], "docstatus": ["<", 2]},
		fields=["schedule_date"],
		order_by="schedule_date desc",
		limit=1,
	)
	return rows[0].schedule_date if rows else None


def _slot_rows(filters: dict) -> list[dict]:
	return [
		dict(r)
		for r in frappe.get_all(
			"MS Timetable Slot",
			filters={**filters, "active": 1},
			fields=["day", "from_time", "to_time", "course", "instructor", "room", "student_group"],
			limit_page_length=0,
		)
	]


def _audience_of(scope: dict, since) -> str:
	"""The audience the lessons being replaced were released to."""
	counts: dict[str, int] = {}
	for a in frappe.get_all(
		"Course Schedule",
		filters={**scope, "schedule_date": [">=", since], "docstatus": ["<", 2]},
		pluck="ms_audience",
		limit_page_length=0,
	):
		counts[a or "draft"] = counts.get(a or "draft", 0) + 1
	if counts:
		return max(counts, key=counts.get)
	# Every lesson was removed (the week emptied, then refilled): fall back to
	# the audience last generated for, so the lessons do not come back hidden.
	stored = frappe.db.get_default(_until_key(scope).replace("until", "audience", 1))
	return stored if stored in AUDIENCES else "draft"


def resync_lessons(scope: dict) -> dict | None:
	"""Carry a saved change to the week into lessons already generated.

	Only when the scope already has lessons from today on — a week never
	generated stays that way until someone presses generate. The past is never
	rewritten, and the lessons keep the audience they were released to, so an
	edit does not quietly unpublish a timetable families can already see.
	"""
	since = getdate(today())
	if not _last_lesson_date(scope, since) and not frappe.db.get_default(_until_key(scope)):
		return None
	last = _generated_until(scope, since)
	if not last or last < since:
		return None
	audience = _audience_of(scope, since)
	try:
		return regenerate_lessons(_slot_rows(scope), scope, since, last, audience)
	except Exception:
		# The week itself is already saved; say that the lessons did not follow
		# rather than letting the save look failed. Generating again is safe —
		# it replaces the same range from the same week.
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Lesson resync failed")
		return {
			"created": 0, "removed": 0, "keptAttended": 0, "keptOver": 0, "skipped": [],
			"error": "حُفظ الجدول، لكن تعذّر تحديث الحصص المولّدة — اضغط «توليد الحصص» لإكمالها.",
		}


def _teacher_range(instructor: str):
	"""The term to generate a teacher's week over: that of the sections taught."""
	groups = frappe.get_all(
		"MS Timetable Slot", filters={"instructor": instructor, "active": 1},
		pluck="student_group", distinct=True,
	)
	ranges = [r for r in (_resolve_range(g, None, None) for g in groups) if r[0] and r[1]]
	if not ranges:
		return None, None
	return min(getdate(r[0]) for r in ranges), max(getdate(r[1]) for r in ranges)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def teacher_lessons_status(instructor: str, persona: str = None):
	"""What generating would cover, and what is already generated."""
	start, end = _teacher_range(instructor)
	since = getdate(today())
	future = frappe.db.count(
		"Course Schedule",
		{"instructor": instructor, "schedule_date": [">=", since], "docstatus": ["<", 2]},
	)
	last = _last_lesson_date({"instructor": instructor}, since)
	return {
		"from": str(start or ""),
		"to": str(end or ""),
		"generated": future,
		"generatedTo": str(last or ""),
		"audience": _audience_of({"instructor": instructor}, since) if future else "draft",
		"slots": frappe.db.count("MS Timetable Slot", {"instructor": instructor, "active": 1}),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def generate_teacher_lessons(
	instructor: str,
	from_date: str = None,
	to_date: str = None,
	audience: str = "draft",
	persona: str = None,
):
	"""Turn one teacher's saved week into dated lessons.

	Replaces only this teacher's lessons in the range — other teachers in the
	same sections are left exactly as they are.
	"""
	if not frappe.db.exists("Instructor", instructor):
		return fail("Instructor not found", "لم يتم العثور على المعلم")
	if audience not in AUDIENCES:
		return fail("Unknown audience", "جمهور غير معروف")
	slots = _slot_rows({"instructor": instructor})
	has_lessons = frappe.db.exists(
		"Course Schedule", {"instructor": instructor, "docstatus": ["<", 2]}
	)
	if not slots and not has_lessons:
		return fail(
			"This teacher has no saved week",
			"لا يوجد جدول محفوظ لهذا المعلم — ابنِ جدوله واحفظه أولاً",
		)
	start, end = (from_date, to_date) if from_date and to_date else _teacher_range(instructor)
	if not start or not end:
		return fail(
			"Could not determine the term dates",
			"تعذّر تحديد تواريخ الفصل — حدّد الفترة يدوياً",
		)
	if getdate(end) < getdate(start):
		return fail("The end date is before the start", "تاريخ النهاية قبل تاريخ البداية")

	result = regenerate_lessons(slots, {"instructor": instructor}, start, end, audience)
	_remember_until({"instructor": instructor}, end)
	_remember_audience({"instructor": instructor}, audience)
	for group in {s["student_group"] for s in slots}:
		sync_group_instructors(group)
	frappe.db.commit()
	result["instructor"] = instructor
	return result


def _timeless(proposed: list[dict]) -> str:
	"""Cells whose period has no time anywhere — named, not thrown.

	Reaching the clash check with an empty time raised a parser error, which
	the screen could only report as "something went wrong".
	"""
	bad = []
	for s in proposed:
		if not (_period_time(s, "from") and _period_time(s, "to")):
			day = dict(sched.WEEKDAYS).get(s.get("day"), s.get("day"))
			bad.append(f"{day} الحصة {cint(s.get('period'))}")
	if not bad:
		return ""
	return (
		"لا وقت محدّد لهذه الحصص: "
		+ "، ".join(sorted(set(bad))[:6])
		+ " — عرّف اليوم الدراسي من شاشة «البناء حسب الشعبة»."
	)
