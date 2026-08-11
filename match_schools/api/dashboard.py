# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Dashboard endpoints — one per persona, shaped for the frontend widgets."""

import frappe
from frappe import _
from frappe.utils import add_months, flt, getdate, today

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	get_default_academic_year,
	ms_endpoint,
	resolve_scope,
)

# Arabic month labels for the trend charts, indexed by calendar month.
ARABIC_MONTHS = {
	1: "كانون ٢",
	2: "شباط",
	3: "آذار",
	4: "نيسان",
	5: "أيار",
	6: "حزيران",
	7: "تموز",
	8: "آب",
	9: "أيلول",
	10: "تشرين ١",
	11: "تشرين ٢",
	12: "كانون ١",
}


@frappe.whitelist()
@ms_endpoint()
def summary(persona: str = None):
	"""Return the dashboard payload for whichever persona is signed in."""
	scope = resolve_scope(persona)
	# The secretary sees the same school-wide view as the admin.
	if persona in BACK_OFFICE:
		return _admin_dashboard()
	if persona == ROLE_TEACHER:
		return _teacher_dashboard(scope)
	if persona == ROLE_STUDENT:
		return _student_dashboard(scope)
	if persona == ROLE_PARENT:
		return _parent_dashboard(scope)
	return {}


# --- Admin -----------------------------------------------------------------


def _admin_dashboard() -> dict:
	academic_year = get_default_academic_year()

	total_students = frappe.db.count("Student", {"enabled": 1})
	total_instructors = frappe.db.count("Instructor")
	group_filters = {"disabled": 0}
	if academic_year:
		group_filters["academic_year"] = academic_year
	total_groups = frappe.db.count("Student Group", group_filters)

	return {
		"kpi": {
			"students": total_students,
			"teachers": total_instructors,
			"classes": total_groups,
			"attendance_today": _attendance_rate_for_date(today()),
		},
		"attendance_trend": _attendance_trend(),
		"grade_distribution": _grade_distribution(academic_year),
		"performance": _performance_by_course(academic_year),
		"gender_split": _gender_split(),
		"fee_collection": _fee_collection_trend(),
		"announcements": _recent_announcements(),
		"academic_year": academic_year,
	}


def _attendance_rate_for_date(date: str) -> float:
	"""Percentage of Present records for a given date."""
	row = frappe.db.sql(
		"""
		SELECT
			COUNT(*) AS total,
			SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance`
		WHERE date = %(date)s AND docstatus < 2
		""",
		{"date": date},
		as_dict=True,
	)
	if not row or not row[0].total:
		return 0.0
	return round(flt(row[0].present) / flt(row[0].total) * 100, 1)


def _attendance_trend(months: int = 8) -> list[dict]:
	"""Monthly present/absent percentages for the last `months` months."""
	start = getdate(add_months(today(), -(months - 1))).replace(day=1)
	rows = frappe.db.sql(
		"""
		SELECT
			YEAR(date) AS yr,
			MONTH(date) AS mo,
			COUNT(*) AS total,
			SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance`
		WHERE date >= %(start)s AND docstatus < 2
		GROUP BY YEAR(date), MONTH(date)
		ORDER BY yr, mo
		""",
		{"start": start},
		as_dict=True,
	)
	trend = []
	for r in rows:
		total = flt(r.total)
		present_pct = round(flt(r.present) / total * 100, 1) if total else 0.0
		trend.append(
			{
				"month": ARABIC_MONTHS.get(r.mo, str(r.mo)),
				"present": present_pct,
				"absent": round(100 - present_pct, 1),
			}
		)
	return trend


def _grade_distribution(academic_year: str | None) -> list[dict]:
	"""Student count per program (a program is a grade level in K-12)."""
	conditions = ["docstatus < 2"]
	params = {}
	if academic_year:
		conditions.append("academic_year = %(academic_year)s")
		params["academic_year"] = academic_year

	rows = frappe.db.sql(
		"""
		SELECT program, COUNT(name) AS students
		FROM `tabProgram Enrollment`
		WHERE {conditions}
		GROUP BY program
		ORDER BY program
		""".format(conditions=" AND ".join(conditions)),
		params,
		as_dict=True,
	)
	return [{"grade": r.program, "students": r.students} for r in rows if r.program]


def _performance_by_course(academic_year: str | None) -> list[dict]:
	"""Average assessment score per course, as a percentage."""
	conditions = ["ar.docstatus = 1", "ar.maximum_score > 0"]
	params = {}
	if academic_year:
		conditions.append("ar.academic_year = %(academic_year)s")
		params["academic_year"] = academic_year

	rows = frappe.db.sql(
		"""
		SELECT ar.course AS course,
			AVG(ar.total_score / ar.maximum_score * 100) AS average
		FROM `tabAssessment Result` ar
		WHERE {conditions}
		GROUP BY ar.course
		ORDER BY average DESC
		LIMIT 8
		""".format(conditions=" AND ".join(conditions)),
		params,
		as_dict=True,
	)
	return [{"subject": r.course, "average": round(flt(r.average), 1)} for r in rows if r.course]


def _gender_split() -> list[dict]:
	rows = frappe.db.sql(
		"""
		SELECT gender, COUNT(name) AS value
		FROM `tabStudent`
		WHERE enabled = 1
		GROUP BY gender
		""",
		as_dict=True,
	)
	return [{"name": r.gender or "غير محدد", "value": r.value} for r in rows]


def _fee_collection_trend(months: int = 6) -> list[dict]:
	"""Expected vs collected fees per month."""
	start = getdate(add_months(today(), -(months - 1))).replace(day=1)
	rows = frappe.db.sql(
		"""
		SELECT
			YEAR(posting_date) AS yr,
			MONTH(posting_date) AS mo,
			SUM(grand_total) AS expected,
			SUM(grand_total - outstanding_amount) AS collected
		FROM `tabSales Invoice`
		WHERE posting_date >= %(start)s AND docstatus = 1
		  AND IFNULL(student, '') != ''
		GROUP BY YEAR(posting_date), MONTH(posting_date)
		ORDER BY yr, mo
		""",
		{"start": start},
		as_dict=True,
	)
	return [
		{
			"month": ARABIC_MONTHS.get(r.mo, str(r.mo)),
			"collected": flt(r.collected),
			"expected": flt(r.expected),
		}
		for r in rows
	]


def _recent_announcements(limit: int = 5) -> list[dict]:
	rows = frappe.get_all(
		"MS Announcement",
		filters={"published": 1},
		fields=["name as id", "title", "body", "posted_on as date", "announcement_type", "audience_label"],
		order_by="posted_on desc",
		limit=limit,
	)
	return [_format_announcement(r) for r in rows]


def _format_announcement(r: dict) -> dict:
	type_labels = {"Announcement": "إعلان", "Event": "حدث", "Alert": "تنبيه"}
	return {
		"id": r.get("id"),
		"title": r.get("title"),
		"body": r.get("body"),
		"date": str(r.get("date") or ""),
		"audience": r.get("audience_label") or "الجميع",
		"type": type_labels.get(r.get("announcement_type"), r.get("announcement_type")),
	}


# --- Teacher ---------------------------------------------------------------


def _teacher_dashboard(scope: dict) -> dict:
	instructor = scope.get("instructor")
	groups = _instructor_groups(instructor)
	group_names = [g["name"] for g in groups]

	students_count = 0
	if group_names:
		students_count = frappe.db.count(
			"Student Group Student",
			{"parent": ["in", group_names], "parenttype": "Student Group", "active": 1},
		)

	pending_grading = frappe.db.count(
		"MS Assignment Submission",
		{"status": "Submitted", "assignment": ["in", _instructor_assignments(instructor) or [""]]},
	)

	return {
		"instructor": instructor,
		"kpi": {
			"classes": len(groups),
			"students": students_count,
			"attendance_today": _attendance_rate_for_groups(group_names, today()),
			"pending_grading": pending_grading,
		},
		"groups": groups,
		"today_schedule": _schedule_for_instructor(instructor, today()),
		"assignments": _assignments_for_instructor(instructor),
		"announcements": _recent_announcements(),
	}


def _instructor_groups(instructor: str | None) -> list[dict]:
	if not instructor:
		return []
	rows = frappe.get_all(
		"Student Group Instructor",
		filters={"instructor": instructor, "parenttype": "Student Group"},
		fields=["parent"],
	)
	names = [r.parent for r in rows]
	if not names:
		return []
	return frappe.get_all(
		"Student Group",
		filters={"name": ["in", names], "disabled": 0},
		fields=["name", "student_group_name", "program", "batch", "course", "academic_year"],
	)


def _instructor_assignments(instructor: str | None) -> list[str]:
	if not instructor:
		return []
	return [
		r.name
		for r in frappe.get_all("MS Assignment", filters={"instructor": instructor}, fields=["name"])
	]


def _attendance_rate_for_groups(groups: list[str], date: str) -> float:
	if not groups:
		return 0.0
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS total,
			SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance`
		WHERE date = %(date)s AND docstatus < 2 AND student_group IN %(groups)s
		""",
		{"date": date, "groups": groups},
		as_dict=True,
	)
	if not row or not row[0].total:
		return 0.0
	return round(flt(row[0].present) / flt(row[0].total) * 100, 1)


def _schedule_for_instructor(instructor: str | None, date: str) -> list[dict]:
	if not instructor:
		return []
	rows = frappe.get_all(
		"Course Schedule",
		filters={"instructor": instructor, "schedule_date": date},
		fields=["name", "course", "student_group", "room", "from_time", "to_time", "title"],
		order_by="from_time",
	)
	return [
		{
			"id": r.name,
			"course": r.course,
			"student_group": r.student_group,
			"room": r.room,
			"from_time": str(r.from_time or ""),
			"to_time": str(r.to_time or ""),
			"title": r.title,
		}
		for r in rows
	]


def _assignments_for_instructor(instructor: str | None, limit: int = 6) -> list[dict]:
	if not instructor:
		return []
	rows = frappe.get_all(
		"MS Assignment",
		filters={"instructor": instructor},
		fields=["name", "title", "course", "student_group", "due_date", "status"],
		order_by="due_date desc",
		limit=limit,
	)
	return [_assignment_card(r) for r in rows]


def _assignment_card(r: dict) -> dict:
	status_labels = {"Open": "مفتوح", "Grading": "قيد التصحيح", "Closed": "مغلق"}
	total = frappe.db.count(
		"Student Group Student",
		{"parent": r.get("student_group"), "parenttype": "Student Group", "active": 1},
	)
	submitted = frappe.db.count("MS Assignment Submission", {"assignment": r.get("name")})
	return {
		"id": r.get("name"),
		"title": r.get("title"),
		"subject": r.get("course"),
		"grade": r.get("student_group"),
		"due": str(r.get("due_date") or ""),
		"submitted": submitted,
		"total": total,
		"status": status_labels.get(r.get("status"), r.get("status")),
	}


# --- Student ---------------------------------------------------------------


def _student_dashboard(scope: dict) -> dict:
	student = scope.get("student")
	if not student:
		return {"student": None}

	return {
		"student": _student_brief(student),
		"kpi": {
			"attendance_rate": _student_attendance_rate(student),
			"average": _student_average(student),
			"pending_assignments": _student_pending_assignments_count(student),
			"outstanding_fees": _student_outstanding_fees(student),
		},
		"today_schedule": _schedule_for_student(student, today()),
		"assignments": _assignments_for_student(student),
		"grades": _student_grades(student),
		"announcements": _recent_announcements(),
	}


def _student_brief(student: str) -> dict:
	doc = frappe.db.get_value(
		"Student", student, ["name", "student_name", "image", "student_email_id"], as_dict=True
	)
	if not doc:
		return {}
	enrollment = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": ["<", 2]},
		fields=["program", "student_batch_name", "academic_year"],
		order_by="creation desc",
		limit=1,
	)
	return {
		"id": doc.name,
		"name": doc.student_name,
		"image": doc.image,
		"email": doc.student_email_id,
		"program": enrollment[0].program if enrollment else None,
		"batch": enrollment[0].student_batch_name if enrollment else None,
		"academic_year": enrollment[0].academic_year if enrollment else None,
	}


def _student_attendance_rate(student: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS total,
			SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance`
		WHERE student = %(student)s AND docstatus < 2
		""",
		{"student": student},
		as_dict=True,
	)
	if not row or not row[0].total:
		return 0.0
	return round(flt(row[0].present) / flt(row[0].total) * 100, 1)


def _student_average(student: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT AVG(total_score / maximum_score * 100) AS average
		FROM `tabAssessment Result`
		WHERE student = %(student)s AND docstatus = 1 AND maximum_score > 0
		""",
		{"student": student},
		as_dict=True,
	)
	return round(flt(row[0].average), 1) if row and row[0].average else 0.0


def _student_groups(student: str) -> list[str]:
	return [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	]


def _student_pending_assignments_count(student: str) -> int:
	groups = _student_groups(student)
	if not groups:
		return 0
	assignments = frappe.get_all(
		"MS Assignment",
		filters={"student_group": ["in", groups], "status": "Open"},
		fields=["name"],
	)
	if not assignments:
		return 0
	names = [a.name for a in assignments]
	submitted = frappe.get_all(
		"MS Assignment Submission",
		filters={"assignment": ["in", names], "student": student},
		fields=["assignment"],
	)
	return len(names) - len({s.assignment for s in submitted})


def _student_outstanding_fees(student: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT SUM(outstanding_amount) AS outstanding
		FROM `tabSales Invoice`
		WHERE student = %(student)s AND docstatus = 1
		""",
		{"student": student},
		as_dict=True,
	)
	return flt(row[0].outstanding) if row else 0.0


def _schedule_for_student(student: str, date: str) -> list[dict]:
	groups = _student_groups(student)
	if not groups:
		return []
	rows = frappe.get_all(
		"Course Schedule",
		filters={"student_group": ["in", groups], "schedule_date": date},
		fields=["name", "course", "instructor_name", "room", "from_time", "to_time", "title"],
		order_by="from_time",
	)
	return [
		{
			"id": r.name,
			"course": r.course,
			"teacher": r.instructor_name,
			"room": r.room,
			"from_time": str(r.from_time or ""),
			"to_time": str(r.to_time or ""),
			"title": r.title,
		}
		for r in rows
	]


def _assignments_for_student(student: str, limit: int = 6) -> list[dict]:
	groups = _student_groups(student)
	if not groups:
		return []
	rows = frappe.get_all(
		"MS Assignment",
		filters={"student_group": ["in", groups]},
		fields=["name", "title", "course", "due_date", "status", "maximum_score"],
		order_by="due_date desc",
		limit=limit,
	)
	out = []
	for r in rows:
		submission = frappe.db.get_value(
			"MS Assignment Submission",
			{"assignment": r.name, "student": student},
			["name", "status", "score"],
			as_dict=True,
		)
		out.append(
			{
				"id": r.name,
				"title": r.title,
				"subject": r.course,
				"due": str(r.due_date or ""),
				"max": flt(r.maximum_score),
				"submitted": bool(submission),
				"submission_status": submission.status if submission else "لم يُسلّم",
				"score": flt(submission.score) if submission and submission.score else None,
			}
		)
	return out


def _student_grades(student: str, limit: int = 8) -> list[dict]:
	rows = frappe.get_all(
		"Assessment Result",
		filters={"student": student, "docstatus": 1},
		fields=["name", "course", "total_score", "maximum_score", "grade", "academic_term"],
		order_by="creation desc",
		limit=limit,
	)
	return [
		{
			"id": r.name,
			"subject": r.course,
			"score": flt(r.total_score),
			"max": flt(r.maximum_score),
			"percentage": round(flt(r.total_score) / flt(r.maximum_score) * 100, 1)
			if flt(r.maximum_score)
			else 0.0,
			"grade": r.grade,
			"term": r.academic_term,
		}
		for r in rows
	]


# --- Parent ----------------------------------------------------------------


def _parent_dashboard(scope: dict) -> dict:
	children = scope.get("students") or []
	payload = []
	for student in children:
		payload.append(
			{
				"student": _student_brief(student),
				"attendance_rate": _student_attendance_rate(student),
				"average": _student_average(student),
				"pending_assignments": _student_pending_assignments_count(student),
				"outstanding_fees": _student_outstanding_fees(student),
				"grades": _student_grades(student, limit=5),
			}
		)
	return {
		"guardian": scope.get("guardian"),
		"children": payload,
		"announcements": _recent_announcements(),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_PARENT, ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def child_overview(student: str, persona: str = None):
	"""Everything about one child, for the parent's focused view.

	A parent picks a child and sees their whole picture in one call rather
	than hopping between screens.
	"""
	scope = resolve_scope(persona)
	if persona == ROLE_PARENT and student not in (scope.get("students") or []):
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	from match_schools.api.gradebook import term_grades
	from match_schools.api.students import _fee_totals

	brief = _student_brief(student)
	grades = term_grades(student=student, persona=persona)
	grade_data = grades.get("data") if isinstance(grades, dict) and "success" in grades else grades
	fees = _fee_totals(student)

	# Behaviour points give parents an at-a-glance conduct summary.
	behaviour = frappe.db.sql(
		"""
		SELECT
			SUM(CASE WHEN record_type = 'Positive' THEN 1 ELSE 0 END) AS positive,
			SUM(CASE WHEN record_type = 'Negative' THEN 1 ELSE 0 END) AS negative,
			SUM(points) AS net_points
		FROM `tabMS Behaviour Record`
		WHERE student = %(student)s
		""",
		{"student": student},
		as_dict=True,
	)[0]

	return {
		"student": brief,
		"kpi": {
			"attendance_rate": _student_attendance_rate(student),
			"average": grade_data.get("overall", 0.0),
			"pending_assignments": _student_pending_assignments_count(student),
			"outstanding_fees": fees["outstanding"],
		},
		"grade": grade_data.get("overall_grade"),
		"subjects": grade_data.get("subjects", []),
		"today_schedule": _schedule_for_student(student, today()),
		"assignments": _assignments_for_student(student, limit=8),
		"attendance": _attendance_breakdown(student),
		"behaviour": {
			"positive": int(behaviour.positive or 0),
			"negative": int(behaviour.negative or 0),
			"net_points": int(behaviour.net_points or 0),
		},
		"fees": {
			"total": fees["total"],
			"paid": fees["paid"],
			"outstanding": fees["outstanding"],
			"status": fees["status"],
		},
		"announcements": _recent_announcements(),
	}


def _attendance_breakdown(student: str) -> dict:
	rows = frappe.db.sql(
		"""
		SELECT status, COUNT(*) AS count
		FROM `tabStudent Attendance`
		WHERE student = %(student)s AND docstatus < 2
		GROUP BY status
		""",
		{"student": student},
		as_dict=True,
	)
	counts = {r.status: r.count for r in rows}
	present = counts.get("Present", 0)
	absent = counts.get("Absent", 0)
	# Excused days are outside the rate entirely — see api/attendance.py.
	excused = counts.get("Excused", 0) + counts.get("Leave", 0)
	total = present + absent
	return {
		"present": present,
		"absent": absent,
		"excused": excused,
		"leave": excused,
		"total": total,
		"rate": round(flt(present) / flt(total) * 100, 1) if total else 0.0,
	}
