# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Assignments and student submissions."""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from match_k12.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	k12_endpoint,
	resolve_scope,
)

STATUS_AR = {"Open": "مفتوح", "Grading": "قيد التصحيح", "Closed": "مغلق"}
SUBMISSION_STATUS_AR = {
	"Submitted": "مُسلّم",
	"Graded": "مُصحّح",
	"Returned": "مُعاد",
	"Late": "متأخر",
}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_assignments(
	student_group: str = None,
	course: str = None,
	status: str = None,
	persona: str = None,
):
	"""Assignments visible to the caller, with submission progress."""
	scope = resolve_scope(persona)
	filters = {}

	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return []
		filters["instructor"] = instructor
	elif persona in (ROLE_STUDENT, ROLE_PARENT):
		groups = _groups_for_students(scope.get("students") or [])
		if not groups:
			return []
		filters["student_group"] = ["in", groups]

	if student_group:
		filters["student_group"] = student_group
	if course:
		filters["course"] = course
	if status:
		filters["status"] = status

	rows = frappe.get_all(
		"K12 Assignment",
		filters=filters,
		fields=[
			"name", "title", "course", "student_group", "program",
			"assigned_on", "due_date", "status", "maximum_score",
			"instructor", "description",
		],
		order_by="due_date desc",
	)

	# For a student/parent view, attach that student's own submission.
	viewer_student = None
	if persona == ROLE_STUDENT:
		viewer_student = scope.get("student")
	elif persona == ROLE_PARENT:
		students = scope.get("students") or []
		viewer_student = students[0] if students else None

	out = []
	for r in rows:
		total = frappe.db.count(
			"Student Group Student",
			{"parent": r.student_group, "parenttype": "Student Group", "active": 1},
		)
		submitted = frappe.db.count("K12 Assignment Submission", {"assignment": r.name})
		item = {
			"id": r.name,
			"title": r.title,
			"subject": r.course,
			"grade": r.program or r.student_group,
			"student_group": r.student_group,
			"due": str(r.due_date or ""),
			"assigned_on": str(r.assigned_on or ""),
			"max": flt(r.maximum_score),
			"submitted": submitted,
			"total": total,
			"status": STATUS_AR.get(r.status, r.status),
			"status_raw": r.status,
			"instructor": r.instructor,
			"description": r.description,
		}
		if viewer_student:
			mine = frappe.db.get_value(
				"K12 Assignment Submission",
				{"assignment": r.name, "student": viewer_student},
				["name", "status", "score", "submitted_on", "feedback"],
				as_dict=True,
			)
			item["my_submission"] = (
				{
					"id": mine.name,
					"status": SUBMISSION_STATUS_AR.get(mine.status, mine.status),
					"status_raw": mine.status,
					"score": flt(mine.score) if mine.score is not None else None,
					"submitted_on": str(mine.submitted_on or ""),
					"feedback": mine.feedback,
				}
				if mine
				else None
			)
		out.append(item)
	return out


def _groups_for_students(students: list[str]) -> list[str]:
	if not students:
		return []
	return list(
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
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def save_assignment(payload: str | dict, persona: str = None):
	"""Create or update an assignment."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	scope = resolve_scope(persona)
	fields = {
		"title": data.get("title"),
		"course": data.get("course"),
		"student_group": data.get("student_group"),
		"due_date": data.get("due_date"),
		"assigned_on": data.get("assigned_on") or today(),
		"maximum_score": data.get("maximum_score") or 100,
		"status": data.get("status") or "Open",
		"description": data.get("description"),
	}
	fields = {k: v for k, v in fields.items() if v is not None}

	assignment_id = data.get("id") or data.get("name")
	if assignment_id:
		doc = frappe.get_doc("K12 Assignment", assignment_id)
		_assert_owns_assignment(doc, persona, scope)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Assignment updated.", "تم تحديث الواجب."
	else:
		missing = [f for f in ("title", "course", "student_group", "due_date") if not fields.get(f)]
		if missing:
			return fail(
				message_en=f"Missing required fields: {', '.join(missing)}.",
				message_ar="بعض الحقول المطلوبة ناقصة.",
			)
		fields["doctype"] = "K12 Assignment"
		if persona == ROLE_TEACHER:
			fields["instructor"] = scope.get("instructor")
		doc = frappe.get_doc(fields)
		doc.insert()
		msg_en, msg_ar = "Assignment created.", "تم إنشاء الواجب."

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.title},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}


def _assert_owns_assignment(doc, persona: str, scope: dict):
	if persona == ROLE_ADMIN:
		return
	if doc.instructor and doc.instructor != scope.get("instructor"):
		frappe.throw(_("You can only edit your own assignments."), frappe.PermissionError)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def submissions(assignment: str, persona: str = None):
	"""All submissions for an assignment, including students who owe one."""
	doc = frappe.get_doc("K12 Assignment", assignment)
	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": doc.student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name"],
		order_by="student_name",
	)
	existing = {
		r.student: r
		for r in frappe.get_all(
			"K12 Assignment Submission",
			filters={"assignment": assignment},
			fields=["name", "student", "status", "score", "submitted_on", "feedback"],
		)
	}

	rows = []
	for s in roster:
		sub = existing.get(s.student)
		rows.append(
			{
				"student": s.student,
				"student_name": s.student_name,
				"submission_id": sub.name if sub else None,
				"status": SUBMISSION_STATUS_AR.get(sub.status, sub.status) if sub else "لم يُسلّم",
				"status_raw": sub.status if sub else None,
				"score": flt(sub.score) if sub and sub.score is not None else None,
				"submitted_on": str(sub.submitted_on or "") if sub else None,
				"feedback": sub.feedback if sub else None,
			}
		)

	return {
		"assignment": {
			"id": doc.name,
			"title": doc.title,
			"subject": doc.course,
			"due": str(doc.due_date or ""),
			"max": flt(doc.maximum_score),
			"student_group": doc.student_group,
		},
		"rows": rows,
		"submitted": len(existing),
		"total": len(roster),
	}


@frappe.whitelist()
@k12_endpoint(ROLE_STUDENT)
def submit_assignment(assignment: str, content: str = None, attachment: str = None, persona: str = None):
	"""A student turns in their work."""
	scope = resolve_scope(persona)
	student = scope.get("student")
	if not student:
		return fail(
			message_en="No student is linked to your account.",
			message_ar="لا يوجد طالب مرتبط بحسابك.",
		)

	# The assignment must belong to one of the student's groups.
	group = frappe.db.get_value("K12 Assignment", assignment, "student_group")
	in_group = frappe.db.exists(
		"Student Group Student",
		{"parent": group, "parenttype": "Student Group", "student": student, "active": 1},
	)
	if not in_group:
		frappe.throw(_("This assignment is not for your class."), frappe.PermissionError)

	existing = frappe.db.get_value(
		"K12 Assignment Submission", {"assignment": assignment, "student": student}, "name"
	)
	if existing:
		doc = frappe.get_doc("K12 Assignment Submission", existing)
		if doc.status in ("Graded", "Returned"):
			return fail(
				message_en="This submission has already been graded.",
				message_ar="تم تصحيح هذا التسليم بالفعل.",
			)
		doc.content = content
		if attachment:
			doc.attachment = attachment
		doc.submitted_on = now_datetime()
		doc.save()
		msg_en, msg_ar = "Submission updated.", "تم تحديث التسليم."
	else:
		doc = frappe.get_doc(
			{
				"doctype": "K12 Assignment Submission",
				"assignment": assignment,
				"student": student,
				"content": content,
				"attachment": attachment,
				"status": "Submitted",
				"submitted_on": now_datetime(),
			}
		)
		doc.insert()
		msg_en, msg_ar = "Assignment submitted.", "تم تسليم الواجب."

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "status": doc.status},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def grade_submission(
	submission: str = None,
	assignment: str = None,
	student: str = None,
	score: float = None,
	feedback: str = None,
	persona: str = None,
):
	"""Record a mark. Creates the submission row if the student never sent one."""
	if not submission and not (assignment and student):
		return fail(
			message_en="Provide a submission id, or an assignment and student.",
			message_ar="يرجى تحديد التسليم أو الواجب والطالب.",
		)

	if submission:
		doc = frappe.get_doc("K12 Assignment Submission", submission)
	else:
		existing = frappe.db.get_value(
			"K12 Assignment Submission", {"assignment": assignment, "student": student}, "name"
		)
		if existing:
			doc = frappe.get_doc("K12 Assignment Submission", existing)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "K12 Assignment Submission",
					"assignment": assignment,
					"student": student,
					"status": "Submitted",
				}
			)
			doc.insert()

	doc.score = flt(score)
	doc.feedback = feedback
	doc.status = "Graded"
	doc.graded_by = frappe.session.user
	doc.graded_on = now_datetime()
	doc.save()
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "score": flt(doc.score)},
		"message_en": "Submission graded.",
		"message_ar": "تم تصحيح التسليم.",
	}
