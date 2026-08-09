
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""One-day departures from the weekly timetable.

The pattern is what the school designed; this is what actually happened on a
given day. A teacher calls in sick and someone covers, a room floods and the
lesson moves, two lessons swap, a day is cancelled.

Each is recorded as an `MS Lesson Change` carrying who was originally
scheduled and why it changed — which is what makes cover reportable
afterwards, rather than the timetable quietly showing a different name with no
explanation.

The weekly pattern is never touched: next week reverts to the design, which is
what a school means by "a substitute for today".
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate, today

from match_schools.api import scheduling as sched
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)

CHANGE_AR = {
	"Substitute": "معلم بديل",
	"Room Change": "تغيير قاعة",
	"Swap": "تبديل حصص",
	"Cancelled": "إلغاء الحصة",
}

REASON_AR = {
	"Sick Leave": "إجازة مرضية",
	"Personal Leave": "إجازة شخصية",
	"Official Duty": "مهمة رسمية",
	"Training": "تدريب",
	"Exam": "امتحان",
	"Other": "أخرى",
}


def _first_conflict(clashes: dict) -> dict | None:
	"""The first clash from a `find_conflicts` result.

	The result is keyed by the lesson's index in the batch, and that key is not
	necessarily 0 — a single-lesson check against existing bookings can report
	under any index. Reading `clashes[0]` directly raises KeyError.
	"""
	for items in clashes.values():
		if items:
			return items[0]
	return None


def _lesson_row(r: dict) -> dict:
	return {
		"id": r.get("name"),
		"date": str(r.get("schedule_date") or ""),
		"studentGroup": r.get("student_group"),
		"course": r.get("course"),
		"instructor": r.get("instructor"),
		"instructorName": r.get("instructor_name"),
		"room": r.get("room"),
		"from": sched.hhmmss(r.get("from_time"))[:5],
		"to": sched.hhmmss(r.get("to_time"))[:5],
		"cancelled": cint(r.get("docstatus")) == 2,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def day_lessons(date: str = None, student_group: str = None, instructor: str = None, persona: str = None):
	"""Every lesson on one day, with any change already applied to it."""
	date = date or today()

	filters: dict = {"schedule_date": date, "docstatus": ["<", 2]}
	if student_group:
		filters["student_group"] = student_group

	# A teacher looking at "my day" should see their own lessons, including
	# any they are covering.
	if persona == ROLE_TEACHER and not student_group and not instructor:
		scope = resolve_scope(persona)
		instructor = scope.get("instructor")
	if instructor:
		filters["instructor"] = instructor

	rows = frappe.get_all(
		"Course Schedule",
		filters=filters,
		fields=[
			"name", "schedule_date", "student_group", "course", "instructor",
			"instructor_name", "room", "from_time", "to_time", "docstatus",
		],
		order_by="from_time",
		limit_page_length=0,
	)

	lessons = [_lesson_row(r) for r in rows]

	# Attach the change record, so the screen can show "covering for X".
	changes = {
		c.course_schedule: c
		for c in frappe.get_all(
			"MS Lesson Change",
			filters={"schedule_date": date, "docstatus": 1},
			fields=[
				"name", "course_schedule", "change_type", "original_instructor",
				"instructor", "original_room", "room", "reason", "notes",
			],
		)
	}

	for lesson in lessons:
		c = changes.get(lesson["id"])
		if not c:
			lesson["change"] = None
			continue
		lesson["change"] = {
			"id": c.name,
			"type": c.change_type,
			"typeLabel": CHANGE_AR.get(c.change_type, c.change_type),
			"originalInstructor": c.original_instructor,
			"originalInstructorName": frappe.db.get_value(
				"Instructor", c.original_instructor, "instructor_name"
			)
			if c.original_instructor
			else None,
			"originalRoom": c.original_room,
			"reason": c.reason,
			"reasonLabel": REASON_AR.get(c.reason, c.reason),
			"notes": c.notes,
		}

	return {
		"date": date,
		"lessons": lessons,
		"changeTypes": [{"value": k, "label": v} for k, v in CHANGE_AR.items()],
		"reasons": [{"value": k, "label": v} for k, v in REASON_AR.items()],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def available_instructors(course_schedule: str, persona: str = None):
	"""Teachers who are free for this lesson's slot.

	Offering only the free ones is the difference between a tool that helps
	and one that lets you create the next conflict.
	"""
	lesson = frappe.db.get_value(
		"Course Schedule",
		course_schedule,
		["schedule_date", "from_time", "to_time", "instructor", "student_group"],
		as_dict=True,
	)
	if not lesson:
		return fail("Lesson not found", "لم يتم العثور على الحصة")

	day = getdate(lesson.schedule_date).strftime("%A")
	start = sched.hhmmss(lesson.from_time)
	end = sched.hhmmss(lesson.to_time)

	everyone = frappe.get_all(
		"Instructor", fields=["name", "instructor_name"], order_by="instructor_name"
	)

	free, busy = [], []
	for teacher in everyone:
		if teacher.name == lesson.instructor:
			continue
		clashes = sched.find_conflicts(
			[
				{
					"day": day,
					"from_time": start,
					"to_time": end,
					"instructor": teacher.name,
					"room": None,
					"student_group": None,
				}
			],
			exclude={course_schedule},
		)
		entry = {"id": teacher.name, "name": teacher.instructor_name or teacher.name}
		first = _first_conflict(clashes)
		if first:
			entry["reason"] = first["detail"]
			busy.append(entry)
		else:
			free.append(entry)

	return {"available": free, "busy": busy}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def record_change(
	course_schedule: str,
	change_type: str,
	instructor: str = None,
	room: str = None,
	reason: str = None,
	notes: str = None,
	persona: str = None,
):
	"""Record and apply a one-day change to a lesson."""
	if not frappe.db.exists("Course Schedule", course_schedule):
		return fail("Lesson not found", "لم يتم العثور على الحصة")
	if change_type not in CHANGE_AR:
		return fail("Unknown change type", "نوع التغيير غير معروف")

	lesson = frappe.db.get_value(
		"Course Schedule",
		course_schedule,
		["schedule_date", "from_time", "to_time", "instructor", "room", "student_group"],
		as_dict=True,
	)

	existing = frappe.db.exists(
		"MS Lesson Change", {"course_schedule": course_schedule, "docstatus": 1}
	)
	if existing:
		return fail(
			"This lesson already has a change ({0})".format(existing),
			"يوجد تغيير مسجّل على هذه الحصة ({0}) — ألغِه أولاً".format(existing),
		)

	# A substitute who is already teaching elsewhere is not a substitute.
	if change_type == "Substitute" and instructor:
		clashes = sched.find_conflicts(
			[
				{
					"day": getdate(lesson.schedule_date).strftime("%A"),
					"from_time": sched.hhmmss(lesson.from_time),
					"to_time": sched.hhmmss(lesson.to_time),
					"instructor": instructor,
					"room": None,
					"student_group": None,
				}
			],
			exclude={course_schedule},
		)
		first = _first_conflict(clashes)
		if first:
			return fail(
				"That teacher is not free: {0}".format(first["detail"]),
				"المعلم غير متاح في هذا الوقت — {0}".format(first["detail"]),
			)

	if change_type == "Room Change" and room:
		clashes = sched.find_conflicts(
			[
				{
					"day": getdate(lesson.schedule_date).strftime("%A"),
					"from_time": sched.hhmmss(lesson.from_time),
					"to_time": sched.hhmmss(lesson.to_time),
					"instructor": None,
					"room": room,
					"student_group": None,
				}
			],
			exclude={course_schedule},
		)
		first = _first_conflict(clashes)
		if first:
			return fail(
				"That room is taken: {0}".format(first["detail"]),
				"القاعة محجوزة في هذا الوقت — {0}".format(first["detail"]),
			)

	if change_type == "Cancelled":
		# Attendance is taken against the lesson, so cancelling one that has a
		# register would strand those rows.
		marked = frappe.db.count(
			"Student Attendance", {"course_schedule": course_schedule, "docstatus": ["<", 2]}
		)
		if marked:
			return fail(
				"Attendance has been taken for this lesson ({0} records)".format(marked),
				"تم رصد الحضور لهذه الحصة ({0} سجلاً) — احذف الحضور أولاً".format(marked),
			)

	doc = frappe.new_doc("MS Lesson Change")
	doc.course_schedule = course_schedule
	doc.schedule_date = lesson.schedule_date
	doc.change_type = change_type
	doc.instructor = instructor
	doc.room = room
	doc.reason = reason
	doc.notes = notes
	doc.insert(ignore_permissions=True)
	doc.submit()
	frappe.db.commit()

	return {
		"id": doc.name,
		"lesson": course_schedule,
		"type": change_type,
		"typeLabel": CHANGE_AR[change_type],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def undo_change(change: str, persona: str = None):
	"""Cancel a recorded change and put the lesson back as designed."""
	if not frappe.db.exists("MS Lesson Change", change):
		return fail("Change not found", "لم يتم العثور على التغيير")

	doc = frappe.get_doc("MS Lesson Change", change)
	if doc.docstatus == 2:
		return fail("Already undone", "تم التراجع عن هذا التغيير مسبقاً")

	lesson = doc.course_schedule
	if frappe.db.exists("Course Schedule", lesson):
		if doc.change_type == "Cancelled":
			frappe.db.set_value("Course Schedule", lesson, "docstatus", 1, update_modified=False)
		if doc.original_instructor:
			frappe.db.set_value("Course Schedule", lesson, "instructor", doc.original_instructor)
		if doc.original_room:
			frappe.db.set_value("Course Schedule", lesson, "room", doc.original_room)

	doc.cancel()
	frappe.db.commit()
	return {"id": change, "lesson": lesson, "restored": True}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def swap_lessons(first: str, second: str, reason: str = None, persona: str = None):
	"""Exchange the teachers of two lessons on the same day.

	Swapping teachers rather than moving lessons keeps each class in its own
	room at its own time, which is what a school does when two colleagues
	trade a period.
	"""
	for name in (first, second):
		if not frappe.db.exists("Course Schedule", name):
			return fail("Lesson not found", "لم يتم العثور على الحصة")

	a = frappe.db.get_value(
		"Course Schedule", first, ["schedule_date", "instructor", "from_time", "to_time"], as_dict=True
	)
	b = frappe.db.get_value(
		"Course Schedule", second, ["schedule_date", "instructor", "from_time", "to_time"], as_dict=True
	)

	if str(a.schedule_date) != str(b.schedule_date):
		return fail(
			"The lessons are on different days",
			"الحصتان في يومين مختلفين — التبديل يكون في نفس اليوم",
		)
	if a.instructor == b.instructor:
		return fail("Both lessons have the same teacher", "الحصتان لنفس المعلم")

	# After swapping, each teacher must be free for the other's slot. Both
	# lessons are excluded because they are the two being rearranged.
	day = getdate(a.schedule_date).strftime("%A")
	both = {first, second}
	for teacher, target in ((a.instructor, b), (b.instructor, a)):
		clashes = sched.find_conflicts(
			[
				{
					"day": day,
					"from_time": sched.hhmmss(target.from_time),
					"to_time": sched.hhmmss(target.to_time),
					"instructor": teacher,
					"room": None,
					"student_group": None,
				}
			],
			exclude=both,
		)
		first = _first_conflict(clashes)
		if first:
			return fail(
				"Swap would clash: {0}".format(first["detail"]),
				"التبديل يسبب تعارضاً — {0}".format(first["detail"]),
			)

	for lesson, new_teacher in ((first, b.instructor), (second, a.instructor)):
		doc = frappe.new_doc("MS Lesson Change")
		doc.course_schedule = lesson
		doc.change_type = "Swap"
		doc.instructor = new_teacher
		doc.reason = reason
		doc.notes = "تبديل مع {0}".format(second if lesson == first else first)
		doc.insert(ignore_permissions=True)
		doc.submit()

	frappe.db.commit()
	return {"first": first, "second": second, "swapped": True}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def cover_report(from_date: str = None, to_date: str = None, persona: str = None):
	"""Who covered for whom, and how often.

	The reason the change record keeps the original teacher: without it there
	is no way to answer "how many periods did Mr Sami cover this term".
	"""
	filters: dict = {"docstatus": 1, "change_type": ["in", ["Substitute", "Swap"]]}
	if from_date and to_date:
		filters["schedule_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["schedule_date"] = [">=", from_date]
	elif to_date:
		filters["schedule_date"] = ["<=", to_date]

	rows = frappe.get_all(
		"MS Lesson Change",
		filters=filters,
		fields=[
			"name", "schedule_date", "change_type", "original_instructor",
			"instructor", "student_group", "course", "reason",
		],
		order_by="schedule_date desc",
		limit_page_length=0,
	)

	names = {r.instructor for r in rows if r.instructor} | {
		r.original_instructor for r in rows if r.original_instructor
	}
	labels = (
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

	covered: dict[str, int] = {}
	absent: dict[str, int] = {}
	for r in rows:
		if r.instructor:
			covered[r.instructor] = covered.get(r.instructor, 0) + 1
		if r.original_instructor:
			absent[r.original_instructor] = absent.get(r.original_instructor, 0) + 1

	return {
		"entries": [
			{
				"id": r.name,
				"date": str(r.schedule_date or ""),
				"type": r.change_type,
				"typeLabel": CHANGE_AR.get(r.change_type, r.change_type),
				"covered": labels.get(r.instructor) or r.instructor,
				"coveredFor": labels.get(r.original_instructor) or r.original_instructor,
				"studentGroup": r.student_group,
				"course": r.course,
				"reason": REASON_AR.get(r.reason, r.reason),
			}
			for r in rows
		],
		"byCovering": sorted(
			[
				{"instructor": labels.get(k) or k, "periods": v}
				for k, v in covered.items()
			],
			key=lambda x: -x["periods"],
		),
		"byAbsent": sorted(
			[{"instructor": labels.get(k) or k, "periods": v} for k, v in absent.items()],
			key=lambda x: -x["periods"],
		),
		"total": len(rows),
	}
