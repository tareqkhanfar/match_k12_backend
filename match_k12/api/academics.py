# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Classes, subjects, teachers, timetable, exams and grades."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from match_k12.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_year,
	k12_endpoint,
	resolve_scope,
)

WEEK_DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


# --- Classes / student groups ---------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def list_classes(program: str = None, academic_year: str = None, persona: str = None):
	filters = {"disabled": 0}
	academic_year = academic_year or get_default_academic_year()
	if academic_year:
		filters["academic_year"] = academic_year
	if program:
		filters["program"] = program

	if persona == ROLE_TEACHER:
		scope = resolve_scope(persona)
		names = [
			r.parent
			for r in frappe.get_all(
				"Student Group Instructor",
				filters={"instructor": scope.get("instructor"), "parenttype": "Student Group"},
				fields=["parent"],
			)
		]
		if not names:
			return []
		filters["name"] = ["in", names]

	groups = frappe.get_all(
		"Student Group",
		filters=filters,
		fields=[
			"name", "student_group_name", "program", "batch", "course",
			"academic_year", "academic_term", "max_strength",
		],
		order_by="student_group_name",
	)

	for g in groups:
		g["students"] = frappe.db.count(
			"Student Group Student",
			{"parent": g["name"], "parenttype": "Student Group", "active": 1},
		)
		g["capacity"] = g.pop("max_strength", None) or 0
		instructors = frappe.get_all(
			"Student Group Instructor",
			filters={"parent": g["name"], "parenttype": "Student Group"},
			fields=["instructor", "instructor_name"],
		)
		g["homeroom"] = instructors[0].instructor_name if instructors else None
		g["instructors"] = [
			{"id": i.instructor, "name": i.instructor_name} for i in instructors
		]
		g["subjects"] = _courses_of_program(g["program"])
	return groups


def _courses_of_program(program: str | None) -> list[str]:
	if not program:
		return []
	return [
		r.course
		for r in frappe.get_all(
			"Program Course",
			filters={"parent": program, "parenttype": "Program"},
			fields=["course"],
			order_by="idx",
		)
	]


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def class_students(student_group: str, persona: str = None):
	rows = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name", "group_roll_number"],
		order_by="group_roll_number, student_name",
	)
	return [
		{"id": r.student, "name": r.student_name, "roll_number": r.group_roll_number}
		for r in rows
	]


# --- Subjects / courses ----------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_subjects(persona: str = None):
	courses = frappe.get_all(
		"Course",
		fields=["name", "course_name", "department"],
		order_by="course_name",
	)
	# Which programs (grades) each course belongs to, and who teaches it.
	for c in courses:
		programs = frappe.get_all(
			"Program Course",
			filters={"course": c["name"], "parenttype": "Program"},
			fields=["parent"],
		)
		c["grades"] = [p.parent for p in programs]
		groups = frappe.get_all(
			"Student Group",
			filters={"course": c["name"], "disabled": 0},
			fields=["name"],
			limit=1,
		)
		teacher = None
		if groups:
			instructor = frappe.get_all(
				"Student Group Instructor",
				filters={"parent": groups[0].name, "parenttype": "Student Group"},
				fields=["instructor_name"],
				limit=1,
			)
			teacher = instructor[0].instructor_name if instructor else None
		c["teacher"] = teacher
		c["id"] = c["name"]
		c["name_ar"] = c["course_name"]
		# v16's Course has no code field; the record name doubles as the code.
		c["code"] = c["name"]
	return courses


# --- Teachers / instructors ------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN)
def list_teachers(search: str = None, persona: str = None):
	filters = {}
	if search:
		filters["instructor_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Instructor",
		filters=filters,
		fields=["name", "instructor_name", "gender", "image", "status", "department", "employee"],
		order_by="instructor_name",
	)
	for r in rows:
		groups = frappe.get_all(
			"Student Group Instructor",
			filters={"instructor": r["name"], "parenttype": "Student Group"},
			fields=["parent"],
		)
		group_names = [g.parent for g in groups]
		r["id"] = r["name"]
		r["name_ar"] = r["instructor_name"]
		r["classes"] = group_names
		r["classes_count"] = len(group_names)
		# Contact details live on the linked Employee, when HR is in use.
		if r.get("employee"):
			emp = frappe.db.get_value(
				"Employee",
				r["employee"],
				["cell_number", "personal_email", "company_email", "date_of_joining"],
				as_dict=True,
			)
			if emp:
				r["phone"] = emp.cell_number
				r["email"] = emp.company_email or emp.personal_email
				r["joined"] = str(emp.date_of_joining or "")
		r.setdefault("phone", None)
		r.setdefault("email", None)
	return rows


# --- Timetable -------------------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def timetable(
	student_group: str = None,
	instructor: str = None,
	student: str = None,
	week_start: str = None,
	persona: str = None,
):
	"""Weekly grid: {day: [slots]} for a group, teacher or student."""
	scope = resolve_scope(persona)
	start = getdate(week_start or today())
	# Normalise to the Sunday that starts the school week.
	start = add_days(start, -((start.weekday() + 1) % 7))
	end = add_days(start, 6)

	filters = {"schedule_date": ["between", [start, end]]}

	if persona == ROLE_TEACHER and not student_group:
		instructor = instructor or scope.get("instructor")
	if persona == ROLE_STUDENT:
		student = scope.get("student")
	if persona == ROLE_PARENT and not student:
		students = scope.get("students") or []
		student = students[0] if students else None

	if student:
		groups = [
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": student, "parenttype": "Student Group", "active": 1},
				fields=["parent"],
			)
		]
		if not groups:
			return {"week_start": str(start), "days": {}}
		filters["student_group"] = ["in", groups]
	elif student_group:
		filters["student_group"] = student_group
	elif instructor:
		filters["instructor"] = instructor

	rows = frappe.get_all(
		"Course Schedule",
		filters=filters,
		fields=[
			"name", "schedule_date", "from_time", "to_time", "course",
			"student_group", "instructor", "instructor_name", "room", "title", "color",
		],
		order_by="schedule_date, from_time",
	)

	days: dict[str, list] = {}
	for r in rows:
		day_label = WEEK_DAYS_AR[getdate(r.schedule_date).weekday()]
		days.setdefault(day_label, []).append(
			{
				"id": r.name,
				"date": str(r.schedule_date),
				"from_time": str(r.from_time or ""),
				"to_time": str(r.to_time or ""),
				"subject": r.course,
				"teacher": r.instructor_name,
				"student_group": r.student_group,
				"room": r.room,
				"title": r.title,
				"color": r.color,
			}
		)

	return {"week_start": str(start), "week_end": str(end), "days": days}


# --- Exams and grades ------------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_exams(academic_term: str = None, program: str = None, persona: str = None):
	"""Assessment Plans act as the exam schedule."""
	filters = {}
	if academic_term:
		filters["academic_term"] = academic_term
	if program:
		filters["program"] = program

	rows = frappe.get_all(
		"Assessment Plan",
		filters=filters,
		fields=[
			"name", "assessment_name", "course", "program", "student_group",
			"schedule_date", "from_time", "to_time", "room", "maximum_assessment_score",
			"assessment_group", "academic_term", "academic_year", "docstatus",
		],
		order_by="schedule_date desc",
	)
	return [
		{
			"id": r.name,
			"title": r.assessment_name,
			"subject": r.course,
			"grade": r.program,
			"student_group": r.student_group,
			"date": str(r.schedule_date or ""),
			"time": str(r.from_time or ""),
			"to_time": str(r.to_time or ""),
			"room": r.room,
			"max": flt(r.maximum_assessment_score),
			"type": r.assessment_group,
			"term": r.academic_term,
			"year": r.academic_year,
			"submitted": r.docstatus == 1,
		}
		for r in rows
	]


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_grades(
	student: str = None,
	student_group: str = None,
	course: str = None,
	persona: str = None,
):
	"""Assessment results, scoped to the caller."""
	scope = resolve_scope(persona)
	filters = {"docstatus": 1}

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return []
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view these grades."), frappe.PermissionError)
		filters["student"] = student if student else ["in", allowed]
	elif student:
		filters["student"] = student

	if student_group:
		filters["student_group"] = student_group
	if course:
		filters["course"] = course

	rows = frappe.get_all(
		"Assessment Result",
		filters=filters,
		fields=[
			"name", "student", "student_name", "course", "total_score",
			"maximum_score", "grade", "academic_term", "academic_year",
			"assessment_plan", "student_group",
		],
		order_by="creation desc",
		limit=500,
	)
	return [
		{
			"id": r.name,
			"student": r.student,
			"student_name": r.student_name,
			"subject": r.course,
			"score": flt(r.total_score),
			"max": flt(r.maximum_score),
			"percentage": round(flt(r.total_score) / flt(r.maximum_score) * 100, 1)
			if flt(r.maximum_score)
			else 0.0,
			"grade": r.grade,
			"term": r.academic_term,
			"year": r.academic_year,
			"exam": r.assessment_plan,
			"student_group": r.student_group,
		}
		for r in rows
	]


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def report_card(student: str, academic_year: str = None, academic_term: str = None, persona: str = None):
	"""Per-subject results plus an overall average for one student."""
	filters = {"student": student, "docstatus": 1}
	if academic_year:
		filters["academic_year"] = academic_year
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"Assessment Result",
		filters=filters,
		fields=["course", "total_score", "maximum_score", "grade", "academic_term"],
		order_by="course",
	)
	if not rows:
		return {"student": student, "subjects": [], "average": 0.0}

	subjects = []
	total_pct = 0.0
	for r in rows:
		pct = round(flt(r.total_score) / flt(r.maximum_score) * 100, 1) if flt(r.maximum_score) else 0.0
		total_pct += pct
		subjects.append(
			{
				"subject": r.course,
				"score": flt(r.total_score),
				"max": flt(r.maximum_score),
				"percentage": pct,
				"grade": r.grade,
				"term": r.academic_term,
			}
		)

	student_doc = frappe.db.get_value(
		"Student", student, ["student_name", "image"], as_dict=True
	) or {}

	return {
		"student": student,
		"student_name": student_doc.get("student_name"),
		"image": student_doc.get("image"),
		"academic_year": academic_year or get_default_academic_year(),
		"academic_term": academic_term,
		"subjects": subjects,
		"average": round(total_pct / len(subjects), 1),
	}
