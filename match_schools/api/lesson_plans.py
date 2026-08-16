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
from frappe.utils import cint

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
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
