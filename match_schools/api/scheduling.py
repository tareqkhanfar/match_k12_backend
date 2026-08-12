
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Conflict detection for the timetable.

Education validates overlaps when a Course Schedule is saved, but it throws on
the first clash it finds. A timetable builder needs the opposite: every
conflict, before anything is written, so a lesson can be shown as invalid
while it is still being dragged.

Three resources can only be in one place at a time:

    instructor      a teacher cannot teach two classes at once
    room            a room cannot hold two classes at once
    student_group   a section cannot sit two lessons at once

A fourth check is availability: a teacher marked unavailable for a slot should
not be scheduled into it, even when nothing else clashes.

Times are compared as strings in HH:MM:SS, which sorts correctly and avoids
the timezone questions that come with datetime objects.
"""

import frappe
from frappe.utils import get_time, getdate

# The teaching week. Sunday-first, matching the region.
WEEKDAYS = [
	("Sunday", "الأحد"),
	("Monday", "الاثنين"),
	("Tuesday", "الثلاثاء"),
	("Wednesday", "الأربعاء"),
	("Thursday", "الخميس"),
	("Friday", "الجمعة"),
	("Saturday", "السبت"),
]
WEEKDAY_AR = dict(WEEKDAYS)

CONFLICT_AR = {
	"instructor": "المعلم مشغول",
	"room": "القاعة محجوزة",
	"student_group": "الشعبة لديها حصة",
	"unavailable": "المعلم غير متاح",
}


def _explain(
	field: str,
	value: str,
	day: str,
	start: str,
	end: str,
	other_course: str | None,
	other_group: str | None,
	other_instructor: str | None,
	other_start: str,
	other_end: str,
	*,
	existing: bool,
) -> str:
	"""Why exactly these two lessons cannot both happen.

	"يتعارض مع حصة أخرى" tells a timetabler nothing they can act on: they still
	have to hunt for the other lesson. This spells out who or what is
	double-booked, on which day, at which two times, and with which class and
	subject — so the fix is obvious from the message alone.
	"""
	day_ar = WEEKDAY_AR.get(day, day)
	when = f"{start[:5]}–{end[:5]}"
	other_when = f"{other_start[:5]}–{other_end[:5]}"
	# Two lessons in the same period read better as one time than as two
	# identical ranges repeated.
	times = when if when == other_when else f"{when} مع {other_when}"

	# What the clashing lesson is, in the school's own words. The class is left
	# out when it is the thing already named as clashing, so the message does
	# not repeat "الصف الأول - أ ... الصف الأول - أ".
	parts = [other_course]
	if field != "student_group":
		parts.append(other_group)
	other_desc = " — ".join([p for p in parts if p]) or "حصة أخرى"

	where = "بحصة محفوظة مسبقاً" if existing else "بحصة أخرى في هذا الجدول"

	if field == "instructor":
		return (
			f"المعلم {value} مرتبط {where}: {other_desc}. "
			f"يوم {day_ar} {times}. "
			"لا يمكن للمعلم أن يكون في شعبتين بنفس الوقت — "
			"غيّر الوقت أو أسند معلماً آخر."
		)

	if field == "room":
		return (
			f"القاعة {value} محجوزة {where}: {other_desc}"
			+ (f" مع {other_instructor}" if other_instructor else "")
			+ f". يوم {day_ar} {times}. اختر قاعة أخرى أو وقتاً آخر."
		)

	# student_group
	return (
		f"الشعبة {value} مرتبطة {where} بنفس الوقت: {other_desc}"
		+ (f" مع {other_instructor}" if other_instructor else "")
		+ f". يوم {day_ar} {times}. "
		"الشعبة لا تدرس مادتين في نفس الحصة — انقل إحداهما لحصة فارغة."
	)


def hhmmss(value) -> str:
	"""Normalise a time to HH:MM:SS so string comparison is safe.

	Frappe hands back `datetime.timedelta` for a Time field, whose str() is
	`8:00:00` — one digit short, which sorts before `10:40:00`. That single
	detail is enough to make a timetable silently wrong.
	"""
	if value is None:
		return ""
	if isinstance(value, str) and len(value) == 8 and value[2] == ":":
		return value
	t = get_time(value)
	return "{:02d}:{:02d}:{:02d}".format(t.hour, t.minute, t.second)


def overlaps(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
	"""True when two half-open intervals share any time.

	Half-open: a lesson ending at 09:00 does not clash with one starting at
	09:00, which is how back-to-back periods actually work.
	"""
	if not (a_from and a_to and b_from and b_to):
		return False
	return a_from < b_to and b_from < a_to


def _weekday(date) -> str:
	return getdate(date).strftime("%A")


def find_conflicts(
	lessons: list[dict],
	*,
	exclude: set[str] | None = None,
	academic_term: str | None = None,
) -> dict[int, list[dict]]:
	"""Every conflict for a proposed set of lessons.

	`lessons` are dicts carrying day, from_time, to_time, instructor, room and
	student_group. Returns a map of the lesson's index to the clashes found,
	so the caller can mark exactly which cell is wrong.

	Checked against both the lessons being proposed and what is already on the
	calendar, because a builder can create two clashing lessons in one pass.
	"""
	exclude = exclude or set()
	booked = _existing_bookings(exclude, academic_term)
	result: dict[int, list[dict]] = {}

	for index, lesson in enumerate(lessons):
		found = []
		day = lesson.get("day")
		start = hhmmss(lesson.get("from_time"))
		end = hhmmss(lesson.get("to_time"))

		if not (day and start and end):
			continue
		if start >= end:
			found.append(
				{
					"kind": "time",
					"label": "وقت غير صالح",
					"detail": "وقت البداية يجب أن يسبق وقت النهاية",
				}
			)

		# Against what is already scheduled.
		for field in ("instructor", "room", "student_group"):
			value = lesson.get(field)
			if not value:
				continue
			for other in booked.get((field, value, day), []):
				if overlaps(start, end, other["from_time"], other["to_time"]):
					found.append(
						{
							"kind": field,
							"label": CONFLICT_AR[field],
							"detail": _explain(
								field,
								value,
								day,
								start,
								end,
								other.get("course"),
								other.get("student_group"),
								other.get("instructor"),
								other["from_time"],
								other["to_time"],
								existing=True,
							),
							"with": other["name"],
						}
					)

		# Against the other lessons in this same request.
		for other_index, other in enumerate(lessons):
			if other_index >= index:
				continue
			if other.get("day") != day:
				continue
			o_start = hhmmss(other.get("from_time"))
			o_end = hhmmss(other.get("to_time"))
			if not overlaps(start, end, o_start, o_end):
				continue
			for field in ("instructor", "room", "student_group"):
				if lesson.get(field) and lesson.get(field) == other.get(field):
					found.append(
						{
							"kind": field,
							"label": CONFLICT_AR[field],
							"detail": _explain(
								field,
								lesson[field],
								day,
								start,
								end,
								other.get("course"),
								other.get("student_group"),
								other.get("instructor"),
								o_start,
								o_end,
								existing=False,
							),
							"with": None,
						}
					)

		# Declared unavailability.
		if lesson.get("instructor"):
			for block in _unavailability(lesson["instructor"], day):
				if overlaps(start, end, block["from_time"], block["to_time"]):
					found.append(
						{
							"kind": "unavailable",
							"label": CONFLICT_AR["unavailable"],
							"detail": block.get("reason") or "غير متاح في هذا الوقت",
						}
					)

		if found:
			result[index] = found

	return result


def _existing_bookings(exclude: set[str], academic_term: str | None) -> dict:
	"""Course Schedules already on the calendar, keyed for quick lookup.

	Keyed by (field, value, weekday) so checking a slot is a dict lookup rather
	than a scan of the whole term.
	"""
	filters: dict = {"docstatus": ["<", 2]}
	if academic_term:
		groups = frappe.get_all(
			"Student Group", filters={"academic_term": academic_term}, pluck="name"
		)
		if groups:
			filters["student_group"] = ["in", groups]

	rows = frappe.get_all(
		"Course Schedule",
		filters=filters,
		fields=[
			"name", "student_group", "instructor", "room", "course",
			"schedule_date", "from_time", "to_time",
		],
		limit_page_length=0,
	)

	index: dict = {}
	for r in rows:
		if r.name in exclude:
			continue
		day = _weekday(r.schedule_date)
		# The class and the teacher travel with the booking so a clash can name
		# the lesson it collides with, not just the time.
		entry = {
			"name": r.name,
			"course": r.course,
			"student_group": r.student_group,
			"instructor": r.instructor,
			"from_time": hhmmss(r.from_time),
			"to_time": hhmmss(r.to_time),
		}
		for field in ("instructor", "room", "student_group"):
			value = r.get(field)
			if value:
				index.setdefault((field, value, day), []).append(entry)
	return index


def _unavailability(instructor: str, day: str) -> list[dict]:
	"""Slots a teacher has been marked unavailable for.

	Read from the app's own availability records; a school that has declared
	none simply has no restrictions.
	"""
	if not frappe.db.table_exists("MS Teacher Availability"):
		return []

	rows = frappe.get_all(
		"MS Teacher Availability",
		filters={"instructor": instructor, "day": day, "available": 0},
		fields=["from_time", "to_time", "reason"],
	)
	return [
		{
			"from_time": hhmmss(r.from_time),
			"to_time": hhmmss(r.to_time),
			"reason": r.reason,
		}
		for r in rows
	]


def summarise(conflicts: dict[int, list[dict]]) -> dict:
	"""A count per kind, for showing a headline before the detail."""
	counts: dict[str, int] = {}
	for items in conflicts.values():
		for c in items:
			counts[c["kind"]] = counts.get(c["kind"], 0) + 1
	return {
		"total": sum(counts.values()),
		"lessons": len(conflicts),
		"byKind": [
			{"kind": k, "label": CONFLICT_AR.get(k, k), "count": v} for k, v in counts.items()
		],
	}
