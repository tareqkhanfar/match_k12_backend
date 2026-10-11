"""Lesson preparation, hung off the timetable cell it belongs to.

A teacher clicking Sunday's first period opens what they prepared for it:
objectives, content, homework, links to material. Families clicking the same
cell see the same thing minus the teacher's private notes, which is the point
— "what did they do in maths today" is currently a question asked at dinner
and answered with "nothing".

A plan belongs to a Course Schedule, not to a weekday and period. The
timetable is rebuilt every week from the pattern, so Sunday period 1 is a
different lesson each week; keying on the lesson is what makes last week's
preparation stay with last week.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	resolve_scope,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)

# Fields a family may read. `notes` is deliberately absent: a teacher needs
# somewhere to write "revisit this, half the class was lost" without it
# becoming a message home.
PUBLIC_FIELDS = (
	"title",
	"objectives",
	"content",
	"homework",
	"resources",
)


def _lesson(schedule: str) -> dict | None:
	return frappe.db.get_value(
		"Course Schedule",
		schedule,
		[
			"name", "student_group", "course", "instructor", "instructor_name",
			"schedule_date", "from_time", "to_time", "docstatus",
		],
		as_dict=True,
	)


def _may_see(persona: str, lesson: dict) -> bool:
	"""Whether this caller is entitled to the lesson at all."""
	if persona in BACK_OFFICE:
		return True
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		# A teacher sees what they teach. Reading another teacher's
		# preparation is not part of the job and is not offered.
		return lesson.get("instructor") == scope.get("instructor")
	students = scope.get("students") or []
	if not students:
		return False
	return bool(
		frappe.db.exists(
			"Student Group Student",
			{
				"parent": lesson.get("student_group"),
				"student": ["in", students],
				"active": 1,
			},
		)
	)


def _may_edit(persona: str, lesson: dict) -> bool:
	"""Only the teacher who takes the lesson prepares it; office staff may fix."""
	if persona in BACK_OFFICE:
		return True
	if persona != ROLE_TEACHER:
		return False
	return lesson.get("instructor") == resolve_scope(persona).get("instructor")


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_lesson_plan(course_schedule: str = None, persona: str = None):
	"""The preparation for one lesson, trimmed to what the caller may read."""
	if not course_schedule:
		return fail(
			message_en="A lesson is required.",
			message_ar="يجب تحديد الحصة.",
		)

	lesson = _lesson(course_schedule)
	if not lesson:
		return fail(
			message_en="Lesson not found.",
			message_ar="لم يتم العثور على الحصة.",
		)
	if not _may_see(persona, lesson):
		frappe.throw(_("This lesson is not yours."), frappe.PermissionError)

	group = frappe.db.get_value(
		"Student Group", lesson.student_group, ["student_group_name", "program", "batch"],
		as_dict=True,
	) or frappe._dict()

	editable = _may_edit(persona, lesson)
	out = {
		"course_schedule": lesson.name,
		"student_group": lesson.student_group,
		"class_name": group.get("student_group_name") or lesson.student_group,
		"program": group.get("program"),
		"batch": group.get("batch"),
		"course": lesson.course,
		"teacher": lesson.instructor_name,
		"date": str(lesson.schedule_date or ""),
		"can_edit": editable,
		"plan": None,
	}

	name = frappe.db.get_value("MS Lesson Plan", {"course_schedule": course_schedule}, "name")
	if not name:
		return out

	doc = frappe.get_doc("MS Lesson Plan", name)
	# A family sees a plan only once the teacher publishes it: preparation in
	# progress on Wednesday for Sunday's lesson is not an announcement.
	if persona in (ROLE_STUDENT, ROLE_PARENT) and not cint(doc.is_published):
		return out

	plan = {f: doc.get(f) for f in PUBLIC_FIELDS}
	plan["id"] = doc.name
	plan["is_published"] = bool(cint(doc.is_published))
	plan["prepared_on"] = str(doc.prepared_on or "")
	if editable:
		plan["notes"] = doc.notes
	out["plan"] = plan
	return out


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_lesson_plan(payload: str | dict = None, persona: str = None):
	"""Create or update the preparation for one lesson."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	schedule = data.get("course_schedule")
	if not schedule:
		return fail(
			message_en="A lesson is required.",
			message_ar="يجب تحديد الحصة.",
		)

	lesson = _lesson(schedule)
	if not lesson:
		return fail(
			message_en="Lesson not found.",
			message_ar="لم يتم العثور على الحصة.",
		)
	if not _may_edit(persona, lesson):
		frappe.throw(_("This lesson is not yours."), frappe.PermissionError)

	name = frappe.db.get_value("MS Lesson Plan", {"course_schedule": schedule}, "name")
	doc = (
		frappe.get_doc("MS Lesson Plan", name)
		if name
		else frappe.new_doc("MS Lesson Plan")
	)

	doc.course_schedule = schedule
	# Copied from the lesson rather than trusted from the caller: these are what
	# the timetable and the family's view filter on.
	doc.student_group = lesson.student_group
	doc.course = lesson.course
	doc.instructor = lesson.instructor
	doc.schedule_date = lesson.schedule_date

	# Every record is anchored to a year and a term. Without them a plan is
	# unfindable the moment a second year exists: "the preparation for Sunday
	# period 1" stops being a question anyone can answer, and reports that
	# filter by term silently miss it.
	group = frappe.db.get_value(
		"Student Group", lesson.student_group, ["academic_year", "academic_term"], as_dict=True
	) or frappe._dict()
	doc.academic_year = group.get("academic_year") or get_default_academic_year()
	doc.academic_term = group.get("academic_term") or get_default_academic_term()

	for field in ("title", "objectives", "content", "homework", "resources", "notes"):
		if field in data:
			doc.set(field, data.get(field))
	if "is_published" in data:
		doc.is_published = cint(data.get("is_published"))

	doc.prepared_by = frappe.session.user
	doc.prepared_on = frappe.utils.now()
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "course_schedule": schedule},
		"message_en": "Lesson plan saved.",
		"message_ar": "تم حفظ تحضير الحصة.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_lesson_plan(course_schedule: str = None, persona: str = None):
	lesson = _lesson(course_schedule) if course_schedule else None
	if not lesson:
		return fail(message_en="Lesson not found.", message_ar="لم يتم العثور على الحصة.")
	if not _may_edit(persona, lesson):
		frappe.throw(_("This lesson is not yours."), frappe.PermissionError)

	name = frappe.db.get_value("MS Lesson Plan", {"course_schedule": course_schedule}, "name")
	if name:
		frappe.delete_doc("MS Lesson Plan", name, ignore_permissions=True, force=True)
		frappe.db.commit()
	return {
		"success": True,
		"data": {"deleted": bool(name)},
		"message_en": "Lesson plan removed.",
		"message_ar": "تم حذف تحضير الحصة.",
	}


def _hhmm(value) -> str:
	"""08:00 from a timedelta or "8:00:00" — str() alone gives "8:00:"."""
	if not value:
		return ""
	parts = str(value).split(":")
	return f"{int(parts[0]):02d}:{parts[1]}" if len(parts) > 1 else str(value)


COPIED_FIELDS = ("title", "objectives", "content", "homework", "resources", "notes")
COPY_WINDOW_DAYS = 7


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def copy_targets(course_schedule: str = None, persona: str = None):
	"""Where this lesson's preparation can be copied: the same teacher's lessons
	of the same subject in their other sections, within a week either side —
	the parallel lesson a teacher of two sections prepares twice."""
	lesson = _lesson(course_schedule) if course_schedule else None
	if not lesson:
		return fail(message_en="Lesson not found.", message_ar="لم يتم العثور على الحصة.")
	if not _may_edit(persona, lesson):
		frappe.throw(_("This lesson is not yours."), frappe.PermissionError)
	date = getdate(lesson.schedule_date)
	rows = frappe.get_all(
		"Course Schedule",
		filters={
			"course": lesson.course,
			"instructor": lesson.instructor,
			"student_group": ["!=", lesson.student_group],
			"schedule_date": ["between", [add_days(date, -COPY_WINDOW_DAYS), add_days(date, COPY_WINDOW_DAYS)]],
			"docstatus": ["<", 2],
		},
		fields=["name", "student_group", "schedule_date", "from_time", "to_time"],
		order_by="schedule_date asc, from_time asc",
		limit_page_length=200,
	)
	names = dict(
		frappe.get_all(
			"Student Group",
			filters={"name": ["in", list({r.student_group for r in rows}) or [""]]},
			fields=["name", "student_group_name"],
			as_list=True,
		)
	)
	planned = set(
		frappe.get_all(
			"MS Lesson Plan", filters={"course_schedule": ["in", [r.name for r in rows] or [""]]}, pluck="course_schedule"
		)
	)
	return {
		"lessons": [
			{
				"course_schedule": r.name,
				"student_group": r.student_group,
				"class_name": names.get(r.student_group) or r.student_group,
				"date": str(r.schedule_date),
				"from": _hhmm(r.from_time),
				"to": _hhmm(r.to_time),
				"hasPlan": r.name in planned,
				"sameDay": getdate(r.schedule_date) == date,
			}
			for r in rows
		]
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def copy_lesson_plan(source: str = None, targets: str | list = None, overwrite: int = 0, persona: str = None):
	"""Copy one lesson's preparation onto other lessons. A lesson that already
	has its own is left alone unless `overwrite` is set. Attachments stay with
	the original."""
	lesson = _lesson(source) if source else None
	name = frappe.db.get_value("MS Lesson Plan", {"course_schedule": source}, "name") if lesson else None
	if not name:
		return fail(message_en="Save this lesson's plan first.", message_ar="احفظ تحضير هذه الحصة أولاً ثم انسخه.")
	if not _may_edit(persona, lesson):
		frappe.throw(_("This lesson is not yours."), frappe.PermissionError)
	src = frappe.get_doc("MS Lesson Plan", name)
	wanted = frappe.parse_json(targets) if isinstance(targets, str) else (targets or [])
	copied, skipped = 0, 0
	for target in wanted:
		other = _lesson(target)
		if not other or not _may_edit(persona, other) or other.course != lesson.course:
			skipped += 1
			continue
		existing = frappe.db.get_value("MS Lesson Plan", {"course_schedule": target}, "name")
		if existing and not cint(overwrite):
			skipped += 1
			continue
		doc = frappe.get_doc("MS Lesson Plan", existing) if existing else frappe.new_doc("MS Lesson Plan")
		doc.course_schedule = target
		doc.student_group = other.student_group
		doc.course = other.course
		doc.instructor = other.instructor
		doc.schedule_date = other.schedule_date
		group = frappe.db.get_value(
			"Student Group", other.student_group, ["academic_year", "academic_term"], as_dict=True
		) or frappe._dict()
		doc.academic_year = group.get("academic_year") or get_default_academic_year()
		doc.academic_term = group.get("academic_term") or get_default_academic_term()
		for field in COPIED_FIELDS:
			doc.set(field, src.get(field))
		doc.is_published = src.is_published
		doc.prepared_by = frappe.session.user
		doc.prepared_on = frappe.utils.now()
		doc.save(ignore_permissions=True)
		copied += 1
	frappe.db.commit()
	return {
		"success": True,
		"data": {"copied": copied, "skipped": skipped},
		"message_en": f"Copied to {copied} lessons.",
		"message_ar": f"نُسخ التحضير إلى {copied} حصة" + (f"، وتُركت {skipped} لها تحضير خاص" if skipped else ""),
	}


def markers_for(schedules: list[str], persona: str) -> dict[str, dict]:
	"""Which of these lessons have preparation, for the timetable's dots.

	Returned as a map so the timetable can mark a cell without a query per
	cell — a week is forty cells and that would be forty round trips.
	"""
	if not schedules:
		return {}
	rows = frappe.get_all(
		"MS Lesson Plan",
		filters={"course_schedule": ["in", schedules]},
		fields=["course_schedule", "is_published", "title", "homework"],
		limit_page_length=0,
	)
	out: dict[str, dict] = {}
	for r in rows:
		published = bool(cint(r.is_published))
		# An unpublished plan is the teacher's own business: the family's cell
		# stays unmarked rather than advertising something they cannot open.
		if persona in (ROLE_STUDENT, ROLE_PARENT) and not published:
			continue
		out[r.course_schedule] = {
			"has_plan": True,
			"published": published,
			"has_homework": bool((r.homework or "").strip()),
			"title": r.title,
		}
	return out
