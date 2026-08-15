# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The exam timetable.

Assessment Plan carries the sitting: subject, class, date, time, room and who
invigilates. This module presents it as a schedule everyone can act on:

  * a student sees only their own class's exams,
  * a teacher sees (and may schedule) exams for the classes and subjects they
    teach,
  * the administration sees and schedules everything.

Marks are not shown here. They live in the gradebook, which is the single
source of truth; an exam's results are carried across with
gradebook.import_exam_results.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from match_schools.api import academic_context as ctx
from match_schools.api.utils import (
	hhmm,
	BACK_OFFICE,
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

# Exam types the school schedules. Assessment Group is a free-form link in
# Education, so these are created on demand rather than assumed to exist.
EXAM_TYPES = [
	("Quiz", "اختبار قصير", "#8b5cf6"),
	("Midterm", "امتحان نصفي", "#0ea5e9"),
	("Final", "امتحان نهائي", "#ef4444"),
	("Practical", "امتحان عملي", "#10b981"),
	("Oral", "امتحان شفوي", "#f59e0b"),
	("Makeup", "امتحان إعادة", "#64748b"),
]
TYPE_AR = {code: label for code, label, _c in EXAM_TYPES}
TYPE_COLOUR = {code: colour for code, _l, colour in EXAM_TYPES}


def _room_names() -> dict[str, str]:
	"""Room ids mapped to readable names — the raw id means nothing to a user."""
	return {
		r.name: (r.room_name or r.name)
		+ (f" ({r.room_number})" if r.room_number else "")
		for r in frappe.get_all(
			"Room", fields=["name", "room_name", "room_number"], limit=500
		)
	}


def _visible_groups(persona: str, scope: dict, student: str = None) -> list[str] | None:
	"""Which classes this persona may see exams for. None means all.

	`student` narrows a family to one child's classes, which is what the
	guardian picked in the header.
	"""
	if persona in BACK_OFFICE:
		return None

	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return []
		groups = {
			r.parent
			for r in frappe.get_all(
				"Student Group Instructor",
				filters={"instructor": instructor, "parenttype": "Student Group"},
				fields=["parent"],
			)
		}
		groups |= {
			r.student_group
			for r in frappe.get_all(
				"Course Schedule",
				filters={"instructor": instructor},
				fields=["student_group"],
				limit=1000,
			)
			if r.student_group
		}
		return sorted(groups)

	# Student or parent: the classes their children are enrolled in.
	students = scope.get("students") or []
	if student:
		if student not in students:
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)
		students = [student]
	if not students:
		return []
	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", students], "parenttype": "Student Group", "active": 1},
				fields=["parent"],
			)
		}
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def schedule(
	academic_term: str = None,
	student_group: str = None,
	student: str = None,
	course: str = None,
	exam_type: str = None,
	from_date: str = None,
	to_date: str = None,
	persona: str = None,
):
	"""The exam timetable, scoped to the caller."""
	scope = resolve_scope(persona)
	groups = _visible_groups(persona, scope, student)
	if groups is not None and not groups:
		return {"exams": [], "upcoming": 0, "past": 0, "types": _type_list()}

	filters = {}
	if groups is not None:
		filters["student_group"] = ["in", groups]
	if student_group:
		filters["student_group"] = student_group
	if course:
		filters["course"] = course
	if academic_term:
		filters["academic_term"] = academic_term
	if from_date and to_date:
		filters["schedule_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["schedule_date"] = [">=", from_date]
	elif to_date:
		filters["schedule_date"] = ["<=", to_date]

	rows = frappe.get_all(
		"Assessment Plan",
		filters=filters,
		fields=[
			"name", "assessment_name", "course", "program", "student_group",
			"schedule_date", "from_time", "to_time", "room", "examiner", "examiner_name",
			"supervisor", "supervisor_name", "maximum_assessment_score",
			"assessment_group", "academic_term", "academic_year", "docstatus",
		],
		order_by="schedule_date, from_time",
		limit=500,
	)

	rooms = _room_names()
	stamp = today()
	exams, upcoming, past = [], 0, 0

	for r in rows:
		date = str(r.schedule_date or "")
		is_upcoming = bool(date and date >= stamp)
		upcoming += 1 if is_upcoming else 0
		past += 0 if is_upcoming else 1

		code = _type_code(r.assessment_group)
		exams.append(
			{
				"id": r.name,
				"title": r.assessment_name,
				"course": r.course,
				"program": r.program,
				"student_group": r.student_group,
				"date": date,
				"from_time": hhmm(r.from_time),
				"to_time": hhmm(r.to_time),
				"duration": _duration(r.from_time, r.to_time),
				"room": r.room,
				"room_name": rooms.get(r.room, r.room),
				"examiner": r.examiner,
				"examiner_name": r.examiner_name,
				"supervisor": r.supervisor,
				"supervisor_name": r.supervisor_name,
				"max": flt(r.maximum_assessment_score),
				"exam_type": code,
				"exam_type_label": TYPE_AR.get(code, r.assessment_group or "—"),
				"colour": TYPE_COLOUR.get(code, "#64748b"),
				"academic_term": r.academic_term,
				"academic_year": r.academic_year,
				"upcoming": is_upcoming,
				"days_away": frappe.utils.date_diff(date, stamp) if date else None,
			}
		)

	if exam_type:
		exams = [e for e in exams if e["exam_type"] == exam_type]

	return {"exams": exams, "upcoming": upcoming, "past": past, "types": _type_list()}


def _type_list() -> list[dict]:
	return [
		{"code": code, "label": label, "colour": colour} for code, label, colour in EXAM_TYPES
	]


def _type_code(assessment_group: str | None) -> str:
	"""Map the stored Assessment Group back to one of our exam types."""
	if not assessment_group:
		return "Final"
	if assessment_group in TYPE_AR:
		return assessment_group
	# Records are named by their Arabic label, so map that back to the code.
	for code, label, _c in EXAM_TYPES:
		if assessment_group == label:
			return code
	# Older rows stored the term name here; treat those as a final exam.
	for code, label, _c in EXAM_TYPES:
		if assessment_group == label:
			return code
	return "Final"


def _duration(from_time, to_time) -> int:
	"""Minutes between two times, for display."""
	if not from_time or not to_time:
		return 0
	try:
		start = frappe.utils.get_time(from_time)
		end = frappe.utils.get_time(to_time)
		return int(
			(end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
		)
	except Exception:
		return 0


# --- Scheduling ------------------------------------------------------------


def _ensure_assessment_group(code: str) -> str:
	"""Assessment Group is a Link, so the exam type must exist as a record.

	Groups are a tree, so a new one has to hang off the root; Education names
	its records by the label, which is what the schedule reads back.
	"""
	label = TYPE_AR.get(code, code)
	if frappe.db.exists("Assessment Group", label):
		return label

	root = frappe.db.get_value(
		"Assessment Group", {"is_group": 1, "parent_assessment_group": ["in", [None, ""]]}, "name"
	) or frappe.db.get_value("Assessment Group", {"is_group": 1}, "name")

	doc = frappe.get_doc(
		{
			"doctype": "Assessment Group",
			"assessment_group_name": label,
			"parent_assessment_group": root,
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _assert_may_schedule(persona: str, student_group: str, course: str):
	"""A teacher may only schedule for the classes and subjects they teach."""
	if persona in BACK_OFFICE:
		return

	from match_schools.api.gradeflow import assert_teacher_owns_course

	assert_teacher_owns_course(persona, course)

	allowed = _visible_groups(persona, resolve_scope(persona)) or []
	if student_group not in allowed:
		frappe.throw(
			_("You can only schedule exams for your own classes."), frappe.PermissionError
		)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def form_options(persona: str = None):
	"""Everything the scheduling form needs."""
	scope = resolve_scope(persona)
	groups = _visible_groups(persona, scope)

	group_filters = {"disabled": 0}
	if groups is not None:
		if not groups:
			return {"groups": [], "courses": [], "rooms": [], "types": _type_list()}
		group_filters["name"] = ["in", groups]

	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import courses_of_instructor

		courses = sorted(courses_of_instructor(scope.get("instructor")))
	else:
		courses = frappe.get_all("Course", pluck="name", limit=300)

	return {
		"groups": [
			{
				"id": g.name,
				"name": g.student_group_name or g.name,
				"program": g.program,
				"students": frappe.db.count(
					"Student Group Student",
					{"parent": g.name, "parenttype": "Student Group", "active": 1},
				),
			}
			for g in frappe.get_all(
				"Student Group",
				filters=group_filters,
				fields=["name", "student_group_name", "program"],
				order_by="student_group_name",
				limit=300,
			)
		],
		"courses": courses,
		"rooms": [
			{
				"id": r.name,
				"name": (r.room_name or r.name)
				+ (f" ({r.room_number})" if r.room_number else ""),
				"capacity": cint(r.seating_capacity),
			}
			for r in frappe.get_all(
				"Room",
				fields=["name", "room_name", "room_number", "seating_capacity"],
				order_by="room_name",
				limit=200,
			)
		],
		"types": _type_list(),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_exam(payload: str | dict, persona: str = None):
	"""Schedule a new exam sitting, or edit one."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	required = ("student_group", "course", "schedule_date")
	missing = [f for f in required if not data.get(f)]
	if missing:
		return fail(
			message_en=f"Missing: {', '.join(missing)}.",
			message_ar="الشعبة والمادة وتاريخ الامتحان مطلوبة.",
		)

	_assert_may_schedule(persona, data["student_group"], data["course"])

	# An exam cannot be sat on a day the school is closed.
	holiday = ctx.holiday_reason(data["schedule_date"])
	if holiday:
		return fail(
			message_en=f"The school is closed on that date ({holiday}).",
			message_ar=f"لا يمكن جدولة امتحان في يوم عطلة — {holiday}.",
		)

	from_time = data.get("from_time")
	to_time = data.get("to_time")
	if from_time and to_time and str(to_time) <= str(from_time):
		return fail(
			message_en="The end time must be after the start time.",
			message_ar="وقت الانتهاء يجب أن يكون بعد وقت البدء.",
		)

	# Two exams cannot share a room at the same moment, and a class cannot sit
	# two exams at once.
	clash = _find_clash(
		data.get("id"),
		data["student_group"],
		data.get("room"),
		data["schedule_date"],
		from_time,
		to_time,
	)
	if clash:
		return fail(message_en=clash["en"], message_ar=clash["ar"])

	group = frappe.db.get_value(
		"Student Group",
		data["student_group"],
		["program", "academic_year", "academic_term"],
		as_dict=True,
	)

	exam_id = data.get("id") or data.get("name")
	if exam_id:
		doc = frappe.get_doc("Assessment Plan", exam_id)
		if doc.docstatus == 1:
			doc.cancel()
			doc = frappe.copy_doc(doc)
	else:
		doc = frappe.new_doc("Assessment Plan")

	code = data.get("exam_type") or "Final"
	doc.assessment_name = data.get("title") or f"{data['course']} - {data['student_group']}"
	doc.student_group = data["student_group"]
	doc.course = data["course"]
	doc.program = group.program if group else None
	doc.academic_year = data.get("academic_year") or (group.academic_year if group else None)
	doc.academic_term = data.get("academic_term") or (group.academic_term if group else None)
	doc.assessment_group = _ensure_assessment_group(code)
	doc.schedule_date = data["schedule_date"]
	doc.from_time = from_time
	doc.to_time = to_time
	doc.room = data.get("room")
	doc.examiner = data.get("examiner")
	doc.supervisor = data.get("supervisor")
	doc.maximum_assessment_score = flt(data.get("max")) or 100
	# Education requires a grading scale on every plan.
	if not doc.grading_scale:
		doc.grading_scale = data.get("grading_scale") or _default_grading_scale()

	# Education requires at least one criterion, weighted to 100.
	if not doc.get("assessment_criteria"):
		criterion = _default_criterion()
		if criterion:
			doc.append(
				"assessment_criteria",
				{"assessment_criteria": criterion, "maximum_score": doc.maximum_assessment_score},
			)

	try:
		doc.save(ignore_permissions=True)
	except frappe.ValidationError as exc:
		# Education runs its own overlap check (against Course Schedule too) and
		# raises before ours can. Surface it as a readable message rather than a
		# raw OverlapError.
		frappe.db.rollback()
		detail = frappe.utils.strip_html(str(exc)).strip()
		return fail(
			message_en=detail or "This time slot is already taken.",
			message_ar=f"تعارض في الموعد — القاعة أو الشعبة مشغولة في هذا الوقت. {detail}".strip(),
		)

	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.assessment_name, "date": str(doc.schedule_date)},
		"message_en": "Exam scheduled.",
		"message_ar": "تم حفظ موعد الامتحان.",
	}


def _default_grading_scale() -> str | None:
	"""Any active scale will do — the schedule never grades against it.

	Education marks `grading_scale` mandatory on Assessment Plan, so a school
	with no scale defined could not schedule an exam at all: every attempt
	failed with a raw "[Assessment Plan, ...]: grading_scale". One is created
	on demand, exactly as `_default_criterion` already does, because the
	timetable has no opinion about grading — it only needs the field filled.
	"""
	existing = frappe.db.get_value(
		"Grading Scale", {"docstatus": 1}, "name"
	) or frappe.db.get_value("Grading Scale", {}, "name")
	if existing:
		return existing

	doc = frappe.get_doc(
		{
			"doctype": "Grading Scale",
			"grading_scale_name": "المقياس الافتراضي",
			"description": "أُنشئ تلقائياً لجدولة الامتحانات.",
			"intervals": [
				{"grade_code": "A", "threshold": 90},
				{"grade_code": "B", "threshold": 80},
				{"grade_code": "C", "threshold": 70},
				{"grade_code": "D", "threshold": 60},
				{"grade_code": "F", "threshold": 0},
			],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _default_criterion() -> str | None:
	"""Education needs a criterion on every plan; reuse or create a generic one."""
	existing = frappe.db.get_value("Assessment Criteria", {}, "name")
	if existing:
		return existing
	doc = frappe.get_doc(
		{"doctype": "Assessment Criteria", "assessment_criteria": "الدرجة الكلية"}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _find_clash(
	exam_id: str | None,
	student_group: str,
	room: str | None,
	date: str,
	from_time,
	to_time,
) -> dict | None:
	"""Refuse a double-booked room, or a class sitting two exams at once."""
	if not (from_time and to_time):
		return None

	candidates = frappe.get_all(
		"Assessment Plan",
		filters={"schedule_date": date, "docstatus": ["<", 2]},
		fields=["name", "student_group", "room", "from_time", "to_time", "course"],
		limit=200,
	)

	for c in candidates:
		if exam_id and c.name == exam_id:
			continue
		if not (c.from_time and c.to_time):
			continue
		# Two intervals overlap unless one ends before the other starts.
		if str(c.to_time) <= str(from_time) or str(c.from_time) >= str(to_time):
			continue

		if c.student_group == student_group:
			return {
				"en": f"This class already sits {c.course} at that time.",
				"ar": f"لدى هذه الشعبة امتحان {c.course} في نفس الوقت.",
			}
		if room and c.room == room:
			return {
				"en": f"That room is taken by {c.course} at that time.",
				"ar": f"القاعة محجوزة لامتحان {c.course} في نفس الوقت.",
			}
	return None


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_exam(exam: str, persona: str = None):
	"""Remove a scheduled exam."""
	doc = frappe.db.get_value(
		"Assessment Plan", exam, ["student_group", "course", "docstatus"], as_dict=True
	)
	if not doc:
		return fail(message_en="Exam not found.", message_ar="لم يتم العثور على الامتحان.")

	_assert_may_schedule(persona, doc.student_group, doc.course)

	if frappe.db.exists("Assessment Result", {"assessment_plan": exam, "docstatus": 1}):
		return fail(
			message_en="This exam has recorded results and cannot be removed.",
			message_ar="لا يمكن حذف امتحان له نتائج مسجّلة.",
		)

	if doc.docstatus == 1:
		frappe.get_doc("Assessment Plan", exam).cancel()
	frappe.delete_doc("Assessment Plan", exam, ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": exam},
		"message_en": "Exam removed.",
		"message_ar": "تم حذف الامتحان من الجدول.",
	}
