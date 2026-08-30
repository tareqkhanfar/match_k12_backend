# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""One call that returns everything the school knows about one person.

The back office used to answer "how is this student doing?" by visiting a
dozen screens and filtering each one. These endpoints gather the same material
in a single response so a head teacher — or a parent sitting across the desk —
sees the whole picture at once.

Reads only. Every screen that edits a record keeps its own endpoint; this
module never becomes a second way to write the same data.
"""

import frappe
from frappe.utils import cint, flt, getdate, nowdate

from match_schools.api.utils import (
	hhmm,
	BACK_OFFICE,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_guardian_students,
	get_linked_guardian,
	get_linked_instructor,
	get_linked_student,
	ms_endpoint,
	resolve_scope,
)

# How many rows each history section carries. Enough to show a pattern
# without turning one response into a full export.
SECTION_LIMIT = 40


# --- Access ----------------------------------------------------------------


def _assert_can_see_student(persona: str, student: str):
	"""A dossier is the widest view of a person in the system.

	Gathering it in one place means one mistake here would expose everything
	at once, so the check is explicit rather than inherited from each section.
	"""
	if persona in BACK_OFFICE:
		return

	if persona == ROLE_STUDENT:
		if get_linked_student() == student:
			return
	elif persona == ROLE_PARENT:
		if student in get_guardian_students(get_linked_guardian()):
			return
	elif persona == ROLE_TEACHER:
		instructor = get_linked_instructor()
		if instructor and _teacher_teaches_student(instructor, student):
			return

	frappe.throw(
		frappe._("You are not allowed to view this student."), frappe.PermissionError
	)


def _teacher_teaches_student(instructor: str, student: str) -> bool:
	"""True when the student sits in any group this instructor teaches."""
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			  FROM `tabStudent Group Student` sgs
			  JOIN `tabStudent Group Instructor` sgi ON sgi.parent = sgs.parent
			 WHERE sgs.student = %(student)s
			   AND sgi.instructor = %(instructor)s
			 LIMIT 1
			""",
			{"student": student, "instructor": instructor},
		)
	)


def _table_exists(doctype: str) -> bool:
	"""Not every deployment installs every module.

	A school without the library module should get a dossier without a library
	section, not a 500.
	"""
	return bool(frappe.db.exists("DocType", doctype))


def _rows(doctype: str, filters: dict, fields: list[str], order_by: str, limit: int = SECTION_LIMIT):
	"""get_all guarded by the doctype actually existing."""
	if not _table_exists(doctype):
		return []
	try:
		return frappe.get_all(
			doctype, filters=filters, fields=fields, order_by=order_by, limit=limit
		)
	except Exception:
		# A module present but mid-migration should not take the page down.
		frappe.log_error(frappe.get_traceback(), f"dossier: reading {doctype} failed")
		return []


# --- Student sections ------------------------------------------------------


def _student_identity(student: str) -> dict | None:
	doc = frappe.db.get_value(
		"Student",
		student,
		[
			"name", "student_name", "gender", "image", "date_of_birth",
			"student_email_id", "student_mobile_number", "joining_date",
			"address_line_1", "city", "nationality", "blood_group", "enabled",
			"user", "middle_name", "last_name",
		],
		as_dict=True,
	)
	if not doc:
		return None

	age = None
	if doc.date_of_birth:
		born = getdate(doc.date_of_birth)
		today = getdate(nowdate())
		age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))

	return {
		"id": doc.name,
		"name": doc.student_name,
		"gender": {"Male": "ذكر", "Female": "أنثى"}.get(doc.gender, doc.gender or ""),
		"image": doc.image,
		"birthDate": str(doc.date_of_birth or ""),
		"age": age,
		"email": doc.student_email_id,
		"phone": doc.student_mobile_number,
		"joined": str(doc.joining_date or ""),
		"address": doc.address_line_1 or "",
		"city": doc.city,
		"nationality": doc.nationality,
		"bloodGroup": doc.blood_group,
		"active": bool(doc.enabled),
		"hasLogin": bool(doc.user),
	}


def _enrollments(student: str) -> list[dict]:
	rows = _rows(
		"Program Enrollment",
		{"student": student, "docstatus": ["<", 2]},
		[
			"name", "program", "academic_year", "academic_term",
			"student_batch_name", "enrollment_date", "docstatus",
		],
		"enrollment_date desc",
	)
	return [
		{
			"id": r.name,
			"program": r.program,
			"academicYear": r.academic_year,
			"academicTerm": r.academic_term,
			"batch": r.student_batch_name,
			"date": str(r.enrollment_date or ""),
			"submitted": cint(r.docstatus) == 1,
		}
		for r in rows
	]


def _guardians(student: str) -> list[dict]:
	links = _rows(
		"Student Guardian",
		{"parent": student, "parenttype": "Student"},
		["guardian", "guardian_name", "relation"],
		"idx asc",
		limit=10,
	)
	out = []
	for link in links:
		detail = frappe.db.get_value(
			"Guardian",
			link.guardian,
			["mobile_number", "email_address", "occupation", "user"],
			as_dict=True,
		) or {}
		out.append(
			{
				"id": link.guardian,
				"name": link.guardian_name,
				"relation": link.relation,
				"phone": detail.get("mobile_number"),
				"email": detail.get("email_address"),
				"occupation": detail.get("occupation"),
				"hasLogin": bool(detail.get("user")),
			}
		)
	return out


def _attendance(student: str) -> dict:
	if not _table_exists("Student Attendance"):
		return {"present": 0, "absent": 0, "late": 0, "total": 0, "rate": 0, "recent": []}

	counts = frappe.db.sql(
		"""
		SELECT status, COUNT(*) AS n
		  FROM `tabStudent Attendance`
		 WHERE student = %(student)s AND docstatus < 2
		 GROUP BY status
		""",
		{"student": student},
		as_dict=True,
	)
	by_status = {c.status: cint(c.n) for c in counts}
	present = by_status.get("Present", 0)
	absent = by_status.get("Absent", 0)
	late = by_status.get("Late", 0)
	# An excused absence is left out of the rate: the school accepted the
	# reason, so it must not appear against the student on a certificate.
	excused = by_status.get("Excused", 0) + by_status.get("Leave", 0)
	total = present + absent + late

	recent = _rows(
		"Student Attendance",
		{"student": student, "docstatus": ["<", 2]},
		["name", "date", "status", "student_group", "course_schedule"],
		"date desc",
		limit=30,
	)

	return {
		"present": present,
		"absent": absent,
		"late": late,
		"excused": excused,
		"total": total,
		"rate": round(present / total * 100, 1) if total else 0,
		"recent": [
			{
				"id": r.name,
				"date": str(r.date or ""),
				"status": r.status,
				"group": r.student_group,
				"lesson": r.course_schedule,
			}
			for r in recent
		],
	}


def _grades(student: str) -> dict:
	results = _rows(
		"Assessment Result",
		{"student": student, "docstatus": ["<", 2]},
		[
			"name", "course", "assessment_plan", "academic_year", "academic_term",
			"total_score", "maximum_score", "grade", "student_group",
		],
		"modified desc",
	)
	entries = _rows(
		"MS Gradebook Entry",
		{"student": student},
		[
			"name", "course", "component_name", "component_type", "score",
			"max_score", "percentage", "academic_term", "program",
		],
		"modified desc",
	)

	scored = [r for r in results if flt(r.maximum_score)]
	average = (
		round(sum(flt(r.total_score) / flt(r.maximum_score) * 100 for r in scored) / len(scored), 1)
		if scored
		else None
	)

	return {
		"average": average,
		"results": [
			{
				"id": r.name,
				"course": r.course,
				"plan": r.assessment_plan,
				"academicYear": r.academic_year,
				"academicTerm": r.academic_term,
				"score": flt(r.total_score),
				"maxScore": flt(r.maximum_score),
				"percentage": (
					round(flt(r.total_score) / flt(r.maximum_score) * 100, 1)
					if flt(r.maximum_score)
					else None
				),
				"grade": r.grade,
				"group": r.student_group,
			}
			for r in results
		],
		"gradebook": [
			{
				"id": e.name,
				"course": e.course,
				"component": e.component_name,
				"type": e.component_type,
				"score": flt(e.score),
				"maxScore": flt(e.max_score),
				"percentage": flt(e.percentage),
				"academicTerm": e.academic_term,
			}
			for e in entries
		],
	}


def _courses(student: str) -> list[dict]:
	rows = _rows(
		"Course Enrollment",
		{"student": student},
		["name", "course", "program", "program_enrollment", "enrollment_date"],
		"enrollment_date desc",
		limit=60,
	)
	return [
		{
			"id": r.name,
			"course": r.course,
			"program": r.program,
			"enrollment": r.program_enrollment,
			"date": str(r.enrollment_date or ""),
		}
		for r in rows
	]


def _behaviour(student: str) -> dict:
	rows = _rows(
		"MS Behaviour Record",
		{"student": student},
		[
			"name", "record_date", "record_type", "points", "category",
			"description", "action_taken", "parent_notified", "reported_by",
		],
		"record_date desc",
	)
	positive = sum(cint(r.points) for r in rows if cint(r.points) > 0)
	negative = sum(cint(r.points) for r in rows if cint(r.points) < 0)
	return {
		"positivePoints": positive,
		"negativePoints": negative,
		"net": positive + negative,
		"records": [
			{
				"id": r.name,
				"date": str(r.record_date or ""),
				"type": r.record_type,
				"points": cint(r.points),
				"category": r.category,
				"description": r.description,
				"action": r.action_taken,
				"parentNotified": bool(r.parent_notified),
				"reportedBy": r.reported_by,
			}
			for r in rows
		],
	}


def _health(student: str) -> dict:
	record = None
	if _table_exists("MS Health Record"):
		record = frappe.db.get_value(
			"MS Health Record",
			{"student": student},
			[
				"name", "blood_group", "height_cm", "weight_kg",
				"chronic_conditions", "allergies", "medications", "special_needs",
				"immunisations", "last_checkup", "emergency_contact_name",
				"emergency_contact_phone", "physician_name", "physician_phone", "notes",
			],
			as_dict=True,
		)

	visits = _rows(
		"MS Health Visit",
		{"student": student},
		["name", "visit_date", "visit_type", "complaint", "treatment", "outcome", "parent_notified"],
		"visit_date desc",
	)

	return {
		"record": (
			{
				"id": record.name,
				"bloodGroup": record.blood_group,
				"heightCm": flt(record.height_cm) or None,
				"weightKg": flt(record.weight_kg) or None,
				"conditions": record.chronic_conditions,
				"allergies": record.allergies,
				"medications": record.medications,
				"specialNeeds": record.special_needs,
				"immunisations": record.immunisations,
				"lastCheckup": str(record.last_checkup or ""),
				"emergencyContact": record.emergency_contact_name,
				"emergencyPhone": record.emergency_contact_phone,
				"physician": record.physician_name,
				"physicianPhone": record.physician_phone,
				"notes": record.notes,
			}
			if record
			else None
		),
		"visits": [
			{
				"id": v.name,
				"date": str(v.visit_date or ""),
				"type": v.visit_type,
				"complaint": v.complaint,
				"treatment": v.treatment,
				"outcome": v.outcome,
				"parentNotified": bool(v.parent_notified),
			}
			for v in visits
		],
	}


# The submission states in Arabic — the dossier is read by people, and
# "Viewed" in an English word tells a head of year nothing.
STATE_AR = {
	"Pending": "لم يُسلّم",
	"Viewed": "اطّلع ولم يُسلّم",
	"Submitted": "سُلّم",
	"Late": "سُلّم متأخراً",
	"Graded": "مُصحّح",
	"Returned": "أُعيد للطالب",
}


def _assignments(student: str) -> dict:
	rows = _rows(
		"MS Assignment Submission",
		{"student": student},
		[
			"name", "assignment", "assignment_title", "status", "submitted_on",
			"score", "maximum_score", "graded_on", "feedback",
		],
		"modified desc",
	)
	from match_schools.api.assignments import HANDED_IN_STATUSES

	# A row exists as soon as the pupil opens the work. Counting those as
	# submitted would overstate a child's record in the one place a school
	# reads when it is deciding something about them.
	graded = [r for r in rows if flt(r.maximum_score) and r.graded_on]
	handed_in = [r for r in rows if r.status in HANDED_IN_STATUSES]
	return {
		"submitted": len(handed_in),
		"opened_not_submitted": sum(1 for r in rows if r.status == "Viewed"),
		"graded": len(graded),
		"averagePercent": (
			round(sum(flt(r.score) / flt(r.maximum_score) * 100 for r in graded) / len(graded), 1)
			if graded
			else None
		),
		"items": [
			{
				"id": r.name,
				"assignment": r.assignment,
				"title": r.assignment_title,
				"status": STATE_AR.get(r.status, r.status),
				"handedIn": r.status in HANDED_IN_STATUSES,
				"submittedOn": str(r.submitted_on or ""),
				"score": flt(r.score) if r.graded_on else None,
				"maxScore": flt(r.maximum_score),
				"feedback": r.feedback,
			}
			for r in rows
		],
	}


def _quizzes(student: str) -> list[dict]:
	rows = _rows(
		"MS Quiz Attempt",
		{"student": student},
		[
			"name", "quiz", "quiz_title", "attempt_number", "status",
			"submitted_on", "score", "total_marks", "percentage", "passed",
		],
		"modified desc",
	)
	return [
		{
			"id": r.name,
			"quiz": r.quiz,
			"title": r.quiz_title,
			"attempt": cint(r.attempt_number),
			"status": r.status,
			"submittedOn": str(r.submitted_on or ""),
			"score": flt(r.score),
			"total": flt(r.total_marks),
			"percentage": flt(r.percentage),
			"passed": bool(r.passed),
		}
		for r in rows
	]


def _billing(student: str) -> dict:
	"""Money owed, read from Sales Invoice.

	The Fees doctype is deliberately not consulted: invoicing moved to Sales
	Invoice, and reading both would double every total.
	"""
	rows = _rows(
		"Sales Invoice",
		{"student": student, "docstatus": ["<", 2]},
		[
			"name", "posting_date", "due_date", "grand_total", "outstanding_amount",
			"status", "docstatus", "ms_program_enrollment",
		],
		"posting_date desc",
		limit=60,
	)
	billed = sum(flt(r.grand_total) for r in rows if cint(r.docstatus) == 1)
	outstanding = sum(flt(r.outstanding_amount) for r in rows if cint(r.docstatus) == 1)
	today = getdate(nowdate())

	return {
		"billed": billed,
		"paid": billed - outstanding,
		"outstanding": outstanding,
		"invoices": [
			{
				"id": r.name,
				"date": str(r.posting_date or ""),
				"dueDate": str(r.due_date or ""),
				"total": flt(r.grand_total),
				"outstanding": flt(r.outstanding_amount),
				"status": r.status,
				"draft": cint(r.docstatus) == 0,
				"overdueDays": (
					(today - getdate(r.due_date)).days
					if r.due_date and flt(r.outstanding_amount) > 0 and getdate(r.due_date) < today
					else 0
				),
				"enrollment": r.ms_program_enrollment,
			}
			for r in rows
		],
	}


def _services(student: str) -> dict:
	"""Library, transport and activities — the non-academic side of the file."""
	loans = _rows(
		"MS Book Loan",
		{"student": student},
		["name", "book", "book_title", "status", "issue_date", "due_date", "return_date"],
		"issue_date desc",
	)
	transport = _rows(
		"MS Transport Assignment",
		{"student": student},
		["name", "route", "stop", "active", "start_date", "end_date"],
		"start_date desc",
		limit=10,
	)
	activities = _rows(
		"MS Activity Enrolment",
		{"student": student},
		["name", "activity", "activity_title", "status", "consent_status", "enrolled_on", "attended"],
		"enrolled_on desc",
	)

	today = getdate(nowdate())
	return {
		"library": [
			{
				"id": r.name,
				"book": r.book_title or r.book,
				"status": r.status,
				"issued": str(r.issue_date or ""),
				"due": str(r.due_date or ""),
				"returned": str(r.return_date or ""),
				"overdue": bool(
					r.due_date and not r.return_date and getdate(r.due_date) < today
				),
			}
			for r in loans
		],
		"transport": [
			{
				"id": r.name,
				"route": r.route,
				"stop": r.stop,
				"active": bool(r.active),
				"from": str(r.start_date or ""),
				"to": str(r.end_date or ""),
			}
			for r in transport
		],
		"activities": [
			{
				"id": r.name,
				"activity": r.activity_title or r.activity,
				"status": r.status,
				"consent": r.consent_status,
				"enrolledOn": str(r.enrolled_on or ""),
				"attended": bool(r.attended),
			}
			for r in activities
		],
	}


def _alerts(student: str) -> list[dict]:
	rows = _rows(
		"MS Student Alert",
		{"student": student},
		["name", "rule_name", "status", "severity", "raised_on", "resolved_on", "title_ar"],
		"raised_on desc",
	)
	return [
		{
			"id": r.name,
			"rule": r.rule_name,
			"title": r.title_ar or r.rule_name,
			"status": r.status,
			"severity": r.severity,
			"raisedOn": str(r.raised_on or ""),
			"resolvedOn": str(r.resolved_on or ""),
		}
		for r in rows
	]


def _timetable(student: str) -> dict:
	"""The student's week, laid out as a grid rather than a list of dates.

	Course Schedule stores one row per dated lesson, so a term holds dozens of
	near-identical rows. A family wants to read "Sunday, period 2, maths" — so
	the lessons are collapsed onto a weekly pattern here, and the raw dated
	rows are returned alongside for anyone who needs the actual calendar.
	"""
	groups = [
		r.parent
		for r in _rows(
			"Student Group Student",
			{"student": student, "parenttype": "Student Group"},
			["parent"],
			"idx asc",
			limit=20,
		)
	]
	empty = {"days": [], "periods": [], "cells": [], "lessons": []}
	if not groups or not _table_exists("Course Schedule"):
		return empty

	rows = frappe.get_all(
		"Course Schedule",
		filters={
			"student_group": ["in", groups],
			# Cancelled lessons stay in the list and are flagged below: a
			# student needs to know a period was called off, not find a hole
			# in their week with no explanation.
			# This is a student's own week; a timetable still in draft is not
			# shown to them, whoever happens to be reading the dossier.
			"ms_audience": "all",
		},
		fields=[
			"name", "course", "schedule_date", "from_time", "to_time",
			"instructor_name", "room", "student_group", "docstatus",
		],
		order_by="schedule_date desc, from_time asc",
		limit=400,
	)
	if not rows:
		return empty

	# One cell per (weekday, start time); the same lesson repeats every week,
	# so the most recent occurrence wins and the rest collapse into it.
	cells: dict[tuple, dict] = {}
	starts: set[str] = set()
	days_seen: set[str] = set()

	for r in rows:
		if not r.schedule_date:
			continue
		day = getdate(r.schedule_date).strftime("%A")
		start = hhmm(r.from_time)
		if not start:
			continue
		days_seen.add(day)
		starts.add(start)
		key = (day, start)
		if key not in cells:
			cells[key] = {
				"day": day,
				"from": start,
				"to": hhmm(r.to_time),
				"course": r.course,
				"instructor": r.instructor_name,
				"room": r.room,
				"group": r.student_group,
				# The weekly grid describes the pattern, and one cancelled date
				# does not change it. The dated list below carries the
				# cancellation, which is where a student looks for "is my
				# lesson on today?".
				"cancelled": cint(r.docstatus) == 2,
			}

	# Keep the school week in order, and only the days that actually have
	# lessons — a school running Sunday to Thursday should not see two empty
	# weekend columns.
	week = [
		"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"
	]
	week_ar = {
		"Sunday": "الأحد",
		"Monday": "الاثنين",
		"Tuesday": "الثلاثاء",
		"Wednesday": "الأربعاء",
		"Thursday": "الخميس",
		"Friday": "الجمعة",
		"Saturday": "السبت",
	}
	days = [
		{"value": d, "label": week_ar[d]} for d in week if d in days_seen
	]

	return {
		"days": days,
		"periods": sorted(starts),
		"cells": list(cells.values()),
		# The dated rows, newest first, for a plain chronological view.
		"lessons": [
			{
				"id": r.name,
				"course": r.course,
				"date": str(r.schedule_date or ""),
				"from": hhmm(r.from_time),
				"to": hhmm(r.to_time),
				"instructor": r.instructor_name,
				"room": r.room,
				"group": r.student_group,
				"cancelled": cint(r.docstatus) == 2,
			}
			for r in rows[:60]
		],
	}


@frappe.whitelist()
@ms_endpoint()
def student_dossier(student: str, persona: str = None):
	"""Everything the school holds on one student, in one response."""
	if not student:
		return fail(
			message_en="A student is required.",
			message_ar="يجب تحديد الطالب.",
		)

	_assert_can_see_student(persona, student)

	identity = _student_identity(student)
	if not identity:
		return fail(
			message_en="Student not found.",
			message_ar="لم يتم العثور على الطالب.",
		)

	enrollments = _enrollments(student)
	current = enrollments[0] if enrollments else None
	identity["grade"] = current["program"] if current else None
	identity["section"] = current["batch"] if current else None
	identity["academicYear"] = current["academicYear"] if current else None

	_grid = _timetable(student)
	_timetable_lessons = _grid.get("lessons", [])
	_timetable_grid = {
		"days": _grid.get("days", []),
		"periods": _grid.get("periods", []),
		"cells": _grid.get("cells", []),
		"lessons": _timetable_lessons,
	}

	return {
		"profile": identity,
		"enrollments": enrollments,
		"guardians": _guardians(student),
		"attendance": _attendance(student),
		"grades": _grades(student),
		"courses": _courses(student),
		"behaviour": _behaviour(student),
		"health": _health(student),
		"assignments": _assignments(student),
		"quizzes": _quizzes(student),
		"billing": _billing(student),
		"services": _services(student),
		"alerts": _alerts(student),
		# `timetable` stays the array of dated lessons that the previous build
		# calls .map on: a browser holding an older bundle cannot be redeployed in
		# step with the server, and changing this shape in place crashed the
		# student profile for exactly that reason. The weekly grid is additive.
		"timetable": _timetable_lessons,
		"timetableGrid": _timetable_grid,
	}


# --- Teacher ---------------------------------------------------------------


def _assert_can_see_instructor(persona: str, instructor: str):
	if persona in BACK_OFFICE:
		return
	if persona == ROLE_TEACHER and get_linked_instructor() == instructor:
		return
	frappe.throw(
		frappe._("You are not allowed to view this teacher."), frappe.PermissionError
	)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def teacher_dossier(instructor: str, persona: str = None):
	"""Everything the school holds on one teacher."""
	if not instructor:
		return fail(
			message_en="A teacher is required.",
			message_ar="يجب تحديد المعلم.",
		)

	_assert_can_see_instructor(persona, instructor)

	doc = frappe.db.get_value(
		"Instructor",
		instructor,
		["name", "instructor_name", "employee", "department", "status", "image", "gender"],
		as_dict=True,
	)
	if not doc:
		return fail(
			message_en="Teacher not found.",
			message_ar="لم يتم العثور على المعلم.",
		)

	# Contact details live on Employee when HR is installed.
	employee = {}
	if doc.employee:
		employee = (
			frappe.db.get_value(
				"Employee",
				doc.employee,
				[
					"personal_email", "company_email", "cell_number", "date_of_joining",
					"designation", "date_of_birth", "user_id",
				],
				as_dict=True,
			)
			or {}
		)

	# The roster alone under-reports: building a timetable never writes to
	# `Student Group Instructor`, so a teacher with a full week of lessons
	# showed no classes at all. Both sources are merged.
	from match_schools.api.academics import _groups_by_instructor

	group_names = _groups_by_instructor([instructor]).get(instructor, [])

	group_detail = []
	student_total = 0
	if group_names:
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", group_names]},
			fields=["name", "student_group_name", "program", "batch", "academic_year", "disabled"],
			limit=60,
		):
			count = frappe.db.count("Student Group Student", {"parent": g.name})
			student_total += count
			group_detail.append(
				{
					"id": g.name,
					"name": g.student_group_name,
					"program": g.program,
					"batch": g.batch,
					"academicYear": g.academic_year,
					"students": count,
					"active": not cint(g.disabled),
				}
			)

	lessons = _rows(
		"Course Schedule",
		{"instructor": instructor, "docstatus": ["<", 2]},
		["name", "course", "schedule_date", "from_time", "to_time", "room", "student_group"],
		"schedule_date desc, from_time asc",
		limit=60,
	)

	# MS Subject Load is a child table of MS Timetable Plan, so it carries the
	# plan on `parent` and has no group or term of its own.
	#
	# A school redrafts its timetable, and every draft keeps its own copy of the
	# loads. Listing them all showed the same subject three times over — once
	# per plan — so only the newest plan per section is kept.
	load_rows = _rows(
		"MS Subject Load",
		{"instructor": instructor, "parenttype": "MS Timetable Plan"},
		["name", "course", "periods_per_week", "max_per_day", "preferred_room", "parent"],
		"idx asc",
		limit=200,
	)

	plans = {}
	if load_rows:
		for plan in frappe.get_all(
			"MS Timetable Plan",
			filters={"name": ["in", list({r.parent for r in load_rows})]},
			fields=["name", "plan_name", "student_group", "status", "modified"],
		):
			plans[plan.name] = plan

	# Newest plan wins for each section.
	current_plan_per_group: dict[str, str] = {}
	for name, plan in sorted(
		plans.items(), key=lambda kv: kv[1].modified or "", reverse=True
	):
		group = plan.student_group or ""
		current_plan_per_group.setdefault(group, name)

	current_plans = set(current_plan_per_group.values())
	loads = [r for r in load_rows if r.parent in current_plans][:40]

	observations = _rows(
		"MS Teacher Observation",
		{"instructor": instructor},
		# The doctype calls these `rating` and `overall_percent`; asking for a
		# column that does not exist made the whole teacher file fail to load.
		["name", "observation_date", "observer", "rating", "overall_percent",
		 "strengths", "status"],
		"observation_date desc",
	)

	assignments = _rows(
		"MS Assignment",
		{"owner": employee.get("user_id") or "__none__"},
		["name", "title", "course", "due_date", "status"],
		"creation desc",
		limit=20,
	) if employee.get("user_id") else []

	return {
		"profile": {
			"id": doc.name,
			"name": doc.instructor_name,
			"employee": doc.employee,
			"department": doc.department,
			"status": doc.status,
			"image": doc.image,
			"gender": {"Male": "ذكر", "Female": "أنثى"}.get(doc.gender, doc.gender or ""),
			"designation": employee.get("designation"),
			"email": employee.get("company_email") or employee.get("personal_email"),
			"phone": employee.get("cell_number"),
			"joined": str(employee.get("date_of_joining") or ""),
			"birthDate": str(employee.get("date_of_birth") or ""),
			"hasLogin": bool(employee.get("user_id")),
		},
		"summary": {
			"groups": len(group_detail),
			"students": student_total,
			"lessons": len(lessons),
			"periodsPerWeek": sum(cint(l.periods_per_week) for l in loads),
		},
		"groups": group_detail,
		"lessons": [
			{
				"id": r.name,
				"course": r.course,
				"date": str(r.schedule_date or ""),
				"from": hhmm(r.from_time),
				"to": hhmm(r.to_time),
				"room": r.room,
				"group": r.student_group,
			}
			for r in lessons
		],
		"loads": [
			{
				"id": r.name,
				"course": r.course,
				"periodsPerWeek": cint(r.periods_per_week),
				"maxPerDay": cint(r.max_per_day),
				"room": r.preferred_room,
				# The section the plan belongs to is what a reader wants here;
				# the plan's own id is kept only so the screen can link to it.
				"section": (plans.get(r.parent) or {}).get("student_group"),
				"planName": (plans.get(r.parent) or {}).get("plan_name"),
				"plan": r.parent,
			}
			for r in loads
		],
		"observations": [
			{
				"id": r.name,
				"date": str(r.observation_date or ""),
				"observer": r.observer,
				"rating": r.rating,
				"percent": flt(r.overall_percent),
				"summary": r.strengths,
				"status": r.status,
			}
			for r in observations
		],
		"assignments": [
			{
				"id": r.name,
				"title": r.title,
				"course": r.course,
				"dueDate": str(r.due_date or ""),
				"status": r.status,
			}
			for r in assignments
		],
	}


# --- Guardian --------------------------------------------------------------


def _assert_can_see_guardian(persona: str, guardian: str):
	"""Who may open a guardian's file.

	A guardian may see their own. Nobody else outside the back office does: a
	teacher has no business reading a parent's ID number or workplace, and one
	family must never reach another's contact details.
	"""
	if persona in BACK_OFFICE:
		return
	if persona == ROLE_PARENT and get_linked_guardian() == guardian:
		return
	frappe.throw(
		frappe._("You are not allowed to view this guardian."), frappe.PermissionError
	)


@frappe.whitelist()
@ms_endpoint()
def guardian_dossier(guardian: str, persona: str = None):
	"""Everything the school holds on one guardian, in one response."""
	if not guardian:
		return fail(
			message_en="A guardian is required.",
			message_ar="يجب تحديد ولي الأمر.",
		)

	_assert_can_see_guardian(persona, guardian)

	doc = frappe.db.get_value(
		"Guardian",
		guardian,
		[
			"name", "guardian_name", "ms_id_number", "email_address", "mobile_number",
			"alternate_number", "date_of_birth", "nationality", "gender", "blood_group",
			"user", "education", "occupation", "designation", "work_address", "image",
		],
		as_dict=True,
	)
	if not doc:
		return fail(
			message_en="Guardian not found.",
			message_ar="لم يتم العثور على ولي الأمر.",
		)

	# The children, each with the state a parent is actually asked about.
	links = _rows(
		"Student Guardian",
		{"guardian": guardian, "parenttype": "Student"},
		["parent", "relation"],
		"idx asc",
		limit=20,
	)

	children = []
	for link in links:
		student = link.parent
		info = frappe.db.get_value(
			"Student", student, ["student_name", "image", "enabled"], as_dict=True
		)
		if not info:
			continue

		enrolment = frappe.db.get_value(
			"Program Enrollment",
			{"student": student, "docstatus": ["<", 2]},
			["program", "student_batch_name", "academic_year"],
			as_dict=True,
			order_by="enrollment_date desc",
		) or {}

		attendance = _attendance(student)
		billing = _billing(student)

		children.append(
			{
				"id": student,
				"name": info.student_name,
				"image": info.image,
				"active": bool(info.enabled),
				"relation": link.relation,
				"program": enrolment.get("program"),
				"batch": enrolment.get("student_batch_name"),
				"academicYear": enrolment.get("academic_year"),
				"attendanceRate": attendance["rate"],
				"absences": attendance["absent"],
				"outstanding": billing["outstanding"],
			}
		)

	outstanding_total = sum(c["outstanding"] for c in children)

	return {
		"profile": {
			"id": doc.name,
			"name": doc.guardian_name,
			"idNumber": doc.ms_id_number,
			"email": doc.email_address,
			"phone": doc.mobile_number,
			"altPhone": doc.alternate_number,
			"birthDate": str(doc.date_of_birth or ""),
			"nationality": doc.nationality,
			"gender": {"Male": "ذكر", "Female": "أنثى"}.get(doc.gender, doc.gender or ""),
			"bloodGroup": doc.blood_group,
			"education": doc.education,
			"occupation": doc.occupation,
			"designation": doc.designation,
			"workAddress": doc.work_address,
			"image": doc.image,
			"hasLogin": bool(doc.user),
		},
		"children": children,
		"summary": {
			"children": len(children),
			"outstanding": round(outstanding_total, 2),
			# The figure a registrar reaches for when a parent is at the desk.
			"needsAttention": sum(1 for c in children if c["outstanding"] > 0),
		},
	}
