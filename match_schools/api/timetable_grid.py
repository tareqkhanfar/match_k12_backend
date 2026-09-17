
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
		"periods": _periods(student_group),
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


def _period_time(slot: dict, edge: str) -> str:
	"""A slot's time, taken from its period definition.

	The period is authoritative: a lesson in period 5 runs when period 5 runs.
	Trusting a time sent alongside the slot let the two drift apart — dragging
	a lesson between periods changed its number but kept its old clock, and the
	stored week then disagreed with the grid that produced it.

	A time supplied for a period the school has not defined is still honoured,
	so a one-off slot outside the standard day is not silently blanked.
	"""
	period = cint(slot.get("period"))
	# This class's own plan when it has one: two sections may run different
	# days, and the globally newest plan would then supply the wrong clock.
	for p in _periods(slot.get("student_group")):
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
			"from_time": _period_time(s, "from"),
			"to_time": _period_time(s, "to"),
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
		doc.from_time = _period_time(s, "from")
		doc.to_time = _period_time(s, "to")
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
	return {"studentGroup": student_group, "slots": len(created)}


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

	removed = 0
	kept_attended = 0
	if cint(replace):
		# Regenerating replaces the untouched lessons but keeps any that were
		# deliberately changed — a substitution is a decision, not noise.
		changed = set(
			frappe.get_all(
				"MS Lesson Change",
				filters={"docstatus": 1},
				pluck="course_schedule",
			)
		)
		candidates = frappe.get_all(
			"Course Schedule",
			filters={
				"student_group": student_group,
				"schedule_date": ["between", [start, end]],
				"docstatus": ["<", 2],
			},
			pluck="name",
		)

		# A lesson that has already been register-marked is a record of what
		# happened, not a plan. Regenerating must not delete it and orphan the
		# attendance rows that point at it.
		attended = set(
			frappe.get_all(
				"Student Attendance",
				filters={"course_schedule": ["in", candidates]} if candidates else {"name": ""},
				pluck="course_schedule",
			)
		)

		doomed = [n for n in candidates if n not in changed and n not in attended]
		kept_attended = len([n for n in candidates if n in attended])

		# Deleted in batches rather than one document at a time. A whole term is
		# well over a thousand lessons, and `delete_doc` per row held a single
		# transaction open long enough to hit MariaDB's 50-second lock wait —
		# the run then failed after ~53s, which over HTTP reached the browser as
		# an unparseable gateway error rather than a message.
		#
		# Course Schedule owns no child tables and nothing links to it here, so
		# a direct delete is equivalent to `delete_doc` minus the per-row
		# document load.
		BATCH = 200
		for i in range(0, len(doomed), BATCH):
			chunk = doomed[i : i + BATCH]
			frappe.db.delete("Course Schedule", {"name": ["in", chunk]})
			removed += len(chunk)
			# Commit each batch so locks are released as we go instead of
			# accumulating across the whole term.
			frappe.db.commit()

	by_day: dict[str, list] = {}
	for s in slots:
		by_day.setdefault(s.day, []).append(s)

	created, skipped = 0, []
	committed_at = 0
	current = getdate(start)
	last = getdate(end)
	while current <= last:
		# The school is shut: no lesson is generated, and the day is reported
		# so the run explains the gap rather than silently missing dates.
		holiday = ctx.holiday_reason(current)
		if holiday:
			skipped.append({"date": str(current), "course": None, "reason": holiday})
			current = add_days(current, 1)
			continue

		for s in by_day.get(current.strftime("%A"), []):
			# Education builds the lesson title as "course by instructor" and
			# crashes on a missing teacher. Refusing here names the subject,
			# instead of failing later with a TypeError nobody can act on.
			if not s.instructor:
				skipped.append(
					{
						"date": str(current),
						"course": s.course,
						"reason": "لا يوجد معلم لهذه المادة — عيّن معلماً قبل التوليد",
					}
				)
				continue

			try:
				doc = frappe.new_doc("Course Schedule")
				doc.student_group = student_group
				doc.course = s.course
				doc.instructor = s.instructor
				# A Link field rejects "" but accepts None. Schools that never
				# record rooms leave this empty, and the room is optional here
				# (see the make_course_schedule_room_optional patch).
				doc.room = s.room or None
				doc.schedule_date = current
				doc.from_time = s.from_time
				doc.to_time = s.to_time
				# Who may read this lesson. Defaults to draft so a week being
				# worked on is not broadcast to families mid-build.
				doc.ms_audience = audience
				if audience != "draft":
					doc.ms_published_on = now_datetime()
				doc.insert(ignore_permissions=True)
				created += 1
			except Exception as exc:
				# Education validates the date against the term and rejects
				# overlaps; report which lesson was refused rather than
				# abandoning the whole run.
				skipped.append(
					{
						"date": str(current),
						"course": s.course,
						"reason": str(exc)[:140],
					}
				)
		# Commit as we go rather than holding one transaction across a whole
		# term, for the same reason the deletes are batched.
		if created - committed_at >= 200:
			frappe.db.commit()
			committed_at = created
		current = add_days(current, 1)

	# الجدول قال من يدرّس هذه الشعبة — نسجّله عليها، وإلا بقي «شعبي»
	# و«طلابي» فارغَين عند من ارتبط بشعبته عبر الجدول وحده.
	sync_group_instructors(student_group)

	frappe.db.commit()
	return {
		"studentGroup": student_group,
		"from": str(start),
		"to": str(end),
		"created": created,
		"removed": removed,
		# Lessons left alone because a register was already taken against them.
		"keptAttended": kept_attended,
		"skipped": skipped,
	}


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

	return {
		"days": [{"value": k, "label": v} for k, v in sched.WEEKDAYS],
		"periods": _periods(None),
		"groups": groups,
		"instructors": frappe.get_all(
			"Instructor",
			filters={"status": "Active"},
			fields=["name", "instructor_name"],
			order_by="instructor_name",
		),
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
	labels = _group_labels({r.student_group for r in rows if r.student_group})
	names = dict(
		frappe.get_all(
			"Instructor",
			filters={"status": ["!=", ""]},
			fields=["name", "instructor_name"],
			as_list=True,
		)
	)
	return {
		"taken": [
			{
				"day": r.day,
				"period": r.period_order,
				"studentGroup": r.student_group,
				"studentGroupName": labels.get(r.student_group, r.student_group),
				"instructor": r.instructor,
				"instructorName": names.get(r.instructor, r.instructor),
				"course": r.course,
				"room": r.room,
			}
			for r in rows
			if not instructor or r.instructor != instructor
		]
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_teacher_pattern(
	instructor: str,
	slots: str | list,
	academic_year: str = None,
	academic_term: str = None,
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
	clashes = []
	seen = {}
	for s in proposed:
		key = (s.get("day"), cint(s.get("period")), s.get("studentGroup"))
		if key in booked:
			other = booked[key]
			who = (
				frappe.db.get_value("Instructor", other.instructor, "instructor_name")
				or other.instructor
			)
			group_label = labels.get(s.get("studentGroup"), s.get("studentGroup"))
			clashes.append(
				"{0}: محجوزة لدى {1}".format(group_label, who)
				if who
				else "{0}: الحصة محجوزة مسبقاً".format(group_label)
			)
		cell = (s.get("day"), cint(s.get("period")))
		if cell in seen:
			clashes.append(
				"{0}: حصتان في الوقت نفسه".format(
					labels.get(s.get("studentGroup"), s.get("studentGroup"))
				)
			)
		seen[cell] = key

	if clashes:
		return fail(
			"The timetable has {0} conflict(s)".format(len(clashes)),
			"تعارضات: " + "، ".join(clashes[:6]),
			data={"clashes": clashes},
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

	frappe.db.commit()
	return {"instructor": instructor, "slots": created, "groups": sorted(g for g in touched if g)}
