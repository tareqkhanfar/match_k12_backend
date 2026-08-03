# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Classes, subjects, teachers, timetable, exams and grades."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from match_k12.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_year,
	k12_endpoint,
	parse_json_arg,
	resolve_scope,
)

WEEK_DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


# --- Classes / student groups ---------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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


# --- Write endpoints: classes / subjects / teachers / exams ----------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_class(payload: str | dict, persona: str = None):
	"""Create or update a Student Group (a class section)."""
	data = parse_json_arg(payload) or {}
	name = data.get("student_group_name")
	if not name and not (data.get("id") or data.get("name")):
		return fail(message_en="Group name is required.", message_ar="اسم الشعبة مطلوب.")

	fields = {
		k: data.get(k)
		for k in (
			"student_group_name", "program", "batch", "course", "academic_year",
			"academic_term", "max_strength", "group_based_on", "disabled",
		)
		if data.get(k) is not None
	}
	fields.setdefault("group_based_on", "Batch")
	fields.setdefault("academic_year", get_default_academic_year())

	group_id = data.get("id") or data.get("name")
	if group_id:
		doc = frappe.get_doc("Student Group", group_id)
		doc.update(fields)
	else:
		doc = frappe.get_doc({"doctype": "Student Group", **fields})

	# Instructors arrive as a list of Instructor ids.
	instructors = data.get("instructors")
	if isinstance(instructors, list):
		doc.set("instructors", [])
		for instructor in instructors:
			if instructor:
				doc.append("instructors", {"instructor": instructor})

	doc.save() if group_id else doc.insert()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Class saved.",
		"message_ar": "تم حفظ الشعبة.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_class(student_group: str, persona: str = None):
	students = frappe.db.count(
		"Student Group Student", {"parent": student_group, "parenttype": "Student Group"}
	)
	if students:
		return fail(
			message_en="This class still has students. Remove them first.",
			message_ar="لا يمكن الحذف: توجد قائمة طلاب في هذه الشعبة.",
		)
	frappe.delete_doc("Student Group", student_group)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": student_group},
		"message_en": "Class deleted.",
		"message_ar": "تم حذف الشعبة.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def set_class_students(student_group: str, students: str | list, persona: str = None):
	"""Replace the roster of a class."""
	student_ids = parse_json_arg(students) or []
	doc = frappe.get_doc("Student Group", student_group)
	doc.set("students", [])
	for idx, student in enumerate(student_ids, start=1):
		name = frappe.db.get_value("Student", student, "student_name")
		doc.append(
			"students",
			{"student": student, "student_name": name, "active": 1, "group_roll_number": idx},
		)
	doc.save()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "count": len(student_ids)},
		"message_en": f"{len(student_ids)} students assigned.",
		"message_ar": f"تم إسناد {len(student_ids)} طالباً.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_subject(payload: str | dict, persona: str = None):
	"""Create or update a Course."""
	data = parse_json_arg(payload) or {}
	if not data.get("course_name") and not (data.get("id") or data.get("name")):
		return fail(message_en="Course name is required.", message_ar="اسم المادة مطلوب.")

	fields = {
		k: data.get(k)
		for k in ("course_name", "department", "description", "default_grading_scale")
		if data.get(k) is not None
	}

	course_id = data.get("id") or data.get("name")
	if course_id:
		doc = frappe.get_doc("Course", course_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Subject updated.", "تم تحديث المادة."
	else:
		doc = frappe.get_doc({"doctype": "Course", **fields})
		doc.insert()
		msg_en, msg_ar = "Subject added.", "تمت إضافة المادة."

	# Optionally attach the course to a set of programs (grades).
	programs = data.get("programs")
	if isinstance(programs, list):
		_sync_course_programs(doc.name, programs)

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


def _sync_course_programs(course: str, programs: list[str]):
	"""Add the course to the given programs and drop it from the others."""
	current = {
		r.parent
		for r in frappe.get_all(
			"Program Course", filters={"course": course, "parenttype": "Program"}, fields=["parent"]
		)
	}
	wanted = {p for p in programs if p}

	for program in wanted - current:
		doc = frappe.get_doc("Program", program)
		doc.append("courses", {"course": course, "required": 1})
		doc.save()

	for program in current - wanted:
		doc = frappe.get_doc("Program", program)
		doc.set("courses", [c for c in doc.courses if c.course != course])
		doc.save()


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_subject(course: str, persona: str = None):
	in_use = frappe.db.count("Student Group", {"course": course})
	if in_use:
		return fail(
			message_en="This subject is used by a class.",
			message_ar="لا يمكن الحذف: المادة مستخدمة في شعبة.",
		)
	frappe.delete_doc("Course", course)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": course},
		"message_en": "Subject deleted.",
		"message_ar": "تم حذف المادة.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_teacher(payload: str | dict, persona: str = None):
	"""Create or update an Instructor."""
	data = parse_json_arg(payload) or {}
	if not data.get("instructor_name") and not (data.get("id") or data.get("name")):
		return fail(message_en="Instructor name is required.", message_ar="اسم المعلم مطلوب.")

	fields = {
		k: data.get(k)
		for k in ("instructor_name", "gender", "status", "department", "employee", "image")
		if data.get(k) is not None
	}
	fields.setdefault("status", "Active")

	instructor_id = data.get("id") or data.get("name")
	if instructor_id:
		doc = frappe.get_doc("Instructor", instructor_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Teacher updated.", "تم تحديث المعلم."
	else:
		doc = frappe.get_doc({"doctype": "Instructor", **fields})
		doc.insert()
		msg_en, msg_ar = "Teacher added.", "تمت إضافة المعلم."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_teacher(instructor: str, persona: str = None):
	assigned = frappe.db.count(
		"Student Group Instructor", {"instructor": instructor, "parenttype": "Student Group"}
	)
	if assigned:
		return fail(
			message_en="This teacher is assigned to a class.",
			message_ar="لا يمكن الحذف: المعلم مسند إلى شعبة.",
		)
	frappe.delete_doc("Instructor", instructor)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": instructor},
		"message_en": "Teacher deleted.",
		"message_ar": "تم حذف المعلم.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_exam(payload: str | dict, persona: str = None):
	"""Create or update an Assessment Plan (an exam)."""
	data = parse_json_arg(payload) or {}
	required = ("assessment_name", "student_group", "course")
	missing = [r for r in required if not data.get(r)]
	if missing and not (data.get("id") or data.get("name")):
		return fail(
			message_en=f"Missing required fields: {', '.join(missing)}.",
			message_ar="بعض الحقول المطلوبة ناقصة.",
		)

	fields = {
		k: data.get(k)
		for k in (
			"assessment_name", "student_group", "course", "program", "academic_year",
			"academic_term", "assessment_group", "grading_scale",
			"maximum_assessment_score", "schedule_date", "from_time", "to_time", "room",
		)
		if data.get(k) is not None
	}
	fields.setdefault("academic_year", get_default_academic_year())
	fields.setdefault("maximum_assessment_score", 100)

	exam_id = data.get("id") or data.get("name")
	if exam_id:
		doc = frappe.get_doc("Assessment Plan", exam_id)
		if doc.docstatus == 1:
			return fail(
				message_en="A submitted exam cannot be edited.",
				message_ar="لا يمكن تعديل امتحان مُعتمد.",
			)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Exam updated.", "تم تحديث الامتحان."
	else:
		doc = frappe.get_doc({"doctype": "Assessment Plan", **fields})
		# Assessment Plan needs at least one criterion.
		criteria = data.get("criteria") or [
			{"assessment_criteria": _default_criteria(), "maximum_score": fields["maximum_assessment_score"]}
		]
		for row in criteria:
			doc.append("assessment_criteria", row)
		doc.insert()
		msg_en, msg_ar = "Exam scheduled.", "تمت جدولة الامتحان."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


def _default_criteria() -> str:
	name = "التقييم العام"
	if not frappe.db.exists("Assessment Criteria", name):
		frappe.get_doc({"doctype": "Assessment Criteria", "assessment_criteria": name}).insert(
			ignore_permissions=True
		)
	return name


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_grade(payload: str | dict, persona: str = None):
	"""Record a student's result for an exam."""
	data = parse_json_arg(payload) or {}
	plan_name = data.get("assessment_plan")
	student = data.get("student")
	if not plan_name or not student:
		return fail(
			message_en="Assessment plan and student are required.",
			message_ar="الامتحان والطالب مطلوبان.",
		)

	plan = frappe.get_doc("Assessment Plan", plan_name)
	score = flt(data.get("score"))
	maximum = flt(plan.maximum_assessment_score) or 100
	if score < 0 or score > maximum:
		return fail(
			message_en=f"Score must be between 0 and {maximum}.",
			message_ar=f"الدرجة يجب أن تكون بين 0 و {maximum}.",
		)

	existing = frappe.db.get_value(
		"Assessment Result",
		{"assessment_plan": plan_name, "student": student, "docstatus": ["<", 2]},
		"name",
	)
	if existing:
		doc = frappe.get_doc("Assessment Result", existing)
		if doc.docstatus == 1:
			doc.cancel()
			doc = frappe.copy_doc(doc)
			doc.amended_from = existing
	else:
		doc = frappe.new_doc("Assessment Result")
		doc.update(
			{
				"assessment_plan": plan_name,
				"student": student,
				"student_group": plan.student_group,
				"course": plan.course,
				"program": plan.program,
				"academic_year": plan.academic_year,
				"academic_term": plan.academic_term,
				"assessment_group": plan.assessment_group,
				"grading_scale": plan.grading_scale,
				"maximum_score": maximum,
			}
		)

	doc.set("details", [])
	criterion = plan.assessment_criteria[0].assessment_criteria if plan.assessment_criteria else _default_criteria()
	doc.append("details", {"assessment_criteria": criterion, "maximum_score": maximum, "score": score})
	doc.comment = data.get("comment")

	doc.save()
	doc.submit()
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "score": flt(doc.total_score), "grade": doc.grade},
		"message_en": "Grade saved.",
		"message_ar": "تم حفظ الدرجة.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def exam_roster(assessment_plan: str, persona: str = None):
	"""The class list for an exam plus any results already entered."""
	plan = frappe.db.get_value(
		"Assessment Plan",
		assessment_plan,
		["name", "assessment_name", "student_group", "course", "maximum_assessment_score"],
		as_dict=True,
	)
	if not plan:
		return fail(message_en="Exam not found.", message_ar="لم يتم العثور على الامتحان.")

	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": plan.student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name"],
		order_by="group_roll_number, student_name",
	)
	results = {
		r.student: r
		for r in frappe.get_all(
			"Assessment Result",
			filters={"assessment_plan": assessment_plan, "docstatus": 1},
			fields=["name", "student", "total_score", "grade", "comment"],
		)
	}

	return {
		"exam": {
			"id": plan.name,
			"title": plan.assessment_name,
			"subject": plan.course,
			"student_group": plan.student_group,
			"max": flt(plan.maximum_assessment_score),
		},
		"rows": [
			{
				"student": s.student,
				"student_name": s.student_name,
				"result_id": results[s.student].name if s.student in results else None,
				"score": flt(results[s.student].total_score) if s.student in results else None,
				"grade": results[s.student].grade if s.student in results else None,
				"comment": results[s.student].comment if s.student in results else None,
			}
			for s in roster
		],
		"entered": len(results),
		"total": len(roster),
	}
