
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Program Enrollment: putting a student into a programme for a year.

Enrolment is what the rest of the system hangs off. Attendance reads it, the
gradebook reads it, a fee invoice is refused without it, and Course
Enrollments are generated from it. Admission creates one automatically; this
module is for everything after that — moving a student between sections,
enrolling a transfer mid-year, or fixing an enrolment made wrongly.

Submitting an enrolment is what makes it real: Education generates the Course
Enrollments on submit. Its `fees` table is deliberately left empty, so
enrolling a child never raises an invoice as a side effect.
"""

import frappe
from frappe import _
from frappe.utils import cint, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)

STATUS_AR = {0: "مسودة", 1: "معتمد", 2: "ملغي"}


def _row(r: dict) -> dict:
	return {
		"id": r.get("name"),
		"student": r.get("student"),
		"studentName": r.get("student_name"),
		"program": r.get("program"),
		"academicYear": r.get("academic_year"),
		"academicTerm": r.get("academic_term"),
		"batch": r.get("student_batch_name"),
		"category": r.get("student_category"),
		"enrolledOn": str(r.get("enrollment_date") or ""),
		"docstatus": cint(r.get("docstatus")),
		"status": STATUS_AR.get(cint(r.get("docstatus")), ""),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_enrollments(
	student: str = None,
	program: str = None,
	academic_year: str = None,
	batch: str = None,
	search: str = None,
	page: int = 1,
	page_size: int = 25,
	persona: str = None,
):
	"""Enrolments the caller may see."""
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 25, 1), 100)

	filters: dict = {"docstatus": ["<", 2]}
	scope = resolve_scope(persona)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return {"items": [], "total": 0, "page": page, "page_size": page_size}
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view this."), frappe.PermissionError)
		filters["student"] = student if student else ["in", allowed]
	elif student:
		filters["student"] = student

	if program:
		filters["program"] = program
	if academic_year:
		filters["academic_year"] = academic_year
	if batch:
		filters["student_batch_name"] = batch

	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [["student_name", "like", like], ["student", "like", like], ["name", "like", like]]

	fields = [
		"name", "student", "student_name", "program", "academic_year",
		"academic_term", "student_batch_name", "student_category",
		"enrollment_date", "docstatus",
	]

	rows = frappe.get_all(
		"Program Enrollment",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="creation desc",
		start=(page - 1) * page_size,
		page_length=page_size,
	)

	return {
		"items": [_row(r) for r in rows],
		"total": frappe.db.count("Program Enrollment", filters),
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def form_options(program: str = None, persona: str = None):
	"""Everything the enrolment form needs.

	Courses are returned per programme so the form can show what the student
	will actually be enrolled in before it is saved.
	"""
	courses = []
	if program:
		courses = [
			{"course": c.course, "required": cint(c.required)}
			for c in frappe.get_all(
				"Program Course",
				filters={"parent": program},
				fields=["course", "required"],
				order_by="idx",
			)
		]

	return {
		"programs": frappe.get_all("Program", pluck="name", order_by="name"),
		"academicYears": frappe.get_all(
			"Academic Year", pluck="name", order_by="year_start_date desc"
		),
		"academicTerms": frappe.get_all(
			"Academic Term", fields=["name", "academic_year"], order_by="name"
		),
		"batches": frappe.get_all("Student Batch Name", pluck="name", order_by="name"),
		"categories": frappe.get_all("Student Category", pluck="name", order_by="name"),
		"defaultAcademicYear": get_default_academic_year(),
		"courses": courses,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_enrollment(payload: str | dict, persona: str = None):
	"""Create or amend an enrolment.

	A submitted enrolment is not edited in place: Course Enrollments and any
	fees already hang off it, so changing it silently would leave those
	pointing at something that no longer matches.
	"""
	data = parse_json_arg(payload) or {}

	student = data.get("student")
	if not student or not frappe.db.exists("Student", student):
		return fail("Student not found", "لم يتم العثور على الطالب")

	program = data.get("program")
	if not program:
		return fail("Program is required", "البرنامج مطلوب")

	academic_year = data.get("academicYear") or get_default_academic_year()
	if not academic_year:
		return fail("Academic year is required", "العام الدراسي مطلوب")

	enrollment_id = data.get("id")

	# Education refuses a duplicate for the same student/programme/year/term,
	# but the message it gives is not helpful, so check first.
	clash = frappe.db.get_value(
		"Program Enrollment",
		{
			"student": student,
			"program": program,
			"academic_year": academic_year,
			"academic_term": data.get("academicTerm") or "",
			"docstatus": ["<", 2],
			"name": ["!=", enrollment_id or ""],
		},
		"name",
	)
	if clash:
		return fail(
			"Already enrolled under {0}".format(clash),
			"الطالب مسجّل مسبقاً في هذا البرنامج لنفس العام ({0})".format(clash),
		)

	if enrollment_id:
		if not frappe.db.exists("Program Enrollment", enrollment_id):
			return fail("Enrollment not found", "لم يتم العثور على التسجيل")
		doc = frappe.get_doc("Program Enrollment", enrollment_id)
		if doc.docstatus == 1:
			return fail(
				"A submitted enrollment cannot be edited; cancel it instead",
				"لا يمكن تعديل تسجيل معتمد — ألغِه وأنشئ تسجيلاً جديداً",
			)
		if doc.docstatus == 2:
			return fail("This enrollment is cancelled", "هذا التسجيل ملغي")
	else:
		doc = frappe.new_doc("Program Enrollment")

	doc.student = student
	doc.student_name = frappe.db.get_value("Student", student, "student_name")
	doc.program = program
	doc.academic_year = academic_year
	doc.academic_term = data.get("academicTerm") or None
	doc.student_batch_name = data.get("batch") or None
	doc.student_category = data.get("category") or None
	doc.enrollment_date = data.get("enrolledOn") or today()

	# The programme's required courses, unless the caller picked specific ones.
	chosen = parse_json_arg(data.get("courses"), None)
	doc.set("courses", [])
	if chosen is None:
		for c in frappe.get_all(
			"Program Course", filters={"parent": program, "required": 1}, fields=["course"]
		):
			doc.append("courses", {"course": c.course})
	else:
		for course in chosen:
			if course:
				doc.append("courses", {"course": course})

	doc.save(ignore_permissions=True)

	if cint(data.get("submit", 1)):
		# Submitting generates the Course Enrollments. `fees` is left empty on
		# purpose, so enrolling never raises an invoice by itself.
		doc.submit()

	frappe.db.commit()
	return _row(doc.as_dict())


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def submit_enrollment(enrollment: str, persona: str = None):
	"""Make a draft enrolment real, generating its Course Enrollments."""
	if not frappe.db.exists("Program Enrollment", enrollment):
		return fail("Enrollment not found", "لم يتم العثور على التسجيل")

	doc = frappe.get_doc("Program Enrollment", enrollment)
	if doc.docstatus == 1:
		return fail("Already submitted", "التسجيل معتمد مسبقاً")
	if doc.docstatus == 2:
		return fail("This enrollment is cancelled", "هذا التسجيل ملغي")

	doc.submit()
	frappe.db.commit()
	return _row(doc.as_dict())


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def cancel_enrollment(enrollment: str, persona: str = None):
	"""Cancel an enrolment, refusing when it is still being relied on."""
	if not frappe.db.exists("Program Enrollment", enrollment):
		return fail("Enrollment not found", "لم يتم العثور على التسجيل")

	doc = frappe.get_doc("Program Enrollment", enrollment)
	if doc.docstatus == 2:
		return fail("Already cancelled", "التسجيل ملغي مسبقاً")

	# An invoice is tied to the enrolment it was raised under; cancelling it
	# from beneath would leave the receivable pointing at nothing.
	invoices = frappe.db.count(
		"Sales Invoice", {"ms_program_enrollment": enrollment, "docstatus": 1}
	)
	if invoices:
		return fail(
			"{0} invoice(s) are linked to this enrollment".format(invoices),
			"يوجد {0} فاتورة مرتبطة بهذا التسجيل — ألغِ الفواتير أولاً".format(invoices),
		)

	if doc.docstatus == 0:
		frappe.delete_doc("Program Enrollment", enrollment, ignore_permissions=True)
		frappe.db.commit()
		return {"id": enrollment, "deleted": True}

	doc.cancel()
	frappe.db.commit()
	return _row(doc.as_dict())


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def enrollment_detail(enrollment: str, persona: str = None):
	"""One enrolment with its courses and anything hanging off it."""
	if not frappe.db.exists("Program Enrollment", enrollment):
		return fail("Enrollment not found", "لم يتم العثور على التسجيل")

	doc = frappe.get_doc("Program Enrollment", enrollment)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		scope = resolve_scope(persona)
		if doc.student not in (scope.get("students") or []):
			frappe.throw(_("You are not allowed to view this."), frappe.PermissionError)

	data = _row(doc.as_dict())
	data["courses"] = [c.course for c in doc.get("courses") or []]
	data["courseEnrollments"] = frappe.get_all(
		"Course Enrollment",
		filters={"program_enrollment": enrollment},
		fields=["name", "course", "enrollment_date"],
		order_by="course",
	)
	# What would block a cancellation, shown up front rather than on failure.
	data["invoices"] = frappe.get_all(
		"Sales Invoice",
		filters={"ms_program_enrollment": enrollment, "docstatus": 1},
		fields=["name", "grand_total", "outstanding_amount"],
	)
	return data
