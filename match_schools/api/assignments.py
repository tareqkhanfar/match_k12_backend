# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Assignments and student submissions."""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from match_schools.api.utils import (
	anchor_term,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	ms_endpoint,
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_assignments(
	student_group: str = None,
	course: str = None,
	status: str = None,
	student: str = None,
	persona: str = None,
):
	"""Assignments visible to the caller, with submission progress.

	`student` narrows a family's view to one child. A guardian with several
	children otherwise sees every child's work in one list, which is not what
	they picked in the header.
	"""
	scope = resolve_scope(persona)
	filters = {}

	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return []
		filters["instructor"] = instructor
	elif persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if student:
			if student not in allowed:
				frappe.throw(
					_("You are not allowed to view this student."), frappe.PermissionError
				)
			allowed = [student]
		groups = _groups_for_students(allowed)
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
		"MS Assignment",
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
		submitted = frappe.db.count("MS Assignment Submission", {"assignment": r.name})
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
				"MS Assignment Submission",
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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

	attachments = _normalise_files(data.get("files"))

	assignment_id = data.get("id") or data.get("name")
	if assignment_id:
		doc = frappe.get_doc("MS Assignment", assignment_id)
		_assert_owns_assignment(doc, persona, scope)
		doc.update(fields)
		if data.get("files") is not None:
			_replace_files(doc, attachments)
		doc.save()
		msg_en, msg_ar = "Assignment updated.", "تم تحديث الواجب."
	else:
		missing = [f for f in ("title", "course", "student_group", "due_date") if not fields.get(f)]
		if missing:
			return fail(
				message_en=f"Missing required fields: {', '.join(missing)}.",
				message_ar="بعض الحقول المطلوبة ناقصة.",
			)
		fields["doctype"] = "MS Assignment"
		if persona == ROLE_TEACHER:
			fields["instructor"] = scope.get("instructor")
		doc = frappe.get_doc(fields)
		_replace_files(doc, attachments)
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def submissions(assignment: str, persona: str = None):
	"""All submissions for an assignment, including students who owe one."""
	doc = frappe.get_doc("MS Assignment", assignment)
	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": doc.student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name"],
		order_by="student_name",
	)
	existing = {
		r.student: r
		for r in frappe.get_all(
			"MS Assignment Submission",
			filters={"assignment": assignment},
			fields=["name", "student", "status", "score", "submitted_on", "feedback", "content"],
		)
	}

	# One query for every attachment, rather than one per submission.
	files_by_submission = {}
	if existing:
		for f in frappe.get_all(
			"MS Attachment",
			filters={
				"parent": ["in", [r.name for r in existing.values()]],
				"parenttype": "MS Assignment Submission",
			},
			fields=["parent", "file_url", "file_name", "file_size"],
			order_by="idx",
		):
			files_by_submission.setdefault(f.parent, []).append(
				{"file_url": f.file_url, "file_name": f.file_name, "file_size": f.file_size}
			)

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
				"content": sub.content if sub else None,
				"files": files_by_submission.get(sub.name, []) if sub else [],
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
@ms_endpoint(ROLE_STUDENT)
def submit_assignment(
	assignment: str,
	content: str = None,
	attachment: str = None,
	files: str | list = None,
	persona: str = None,
):
	"""A student turns in their work: rich text plus any number of files."""
	scope = resolve_scope(persona)
	student = scope.get("student")
	if not student:
		return fail(
			message_en="No student is linked to your account.",
			message_ar="لا يوجد طالب مرتبط بحسابك.",
		)

	# The assignment must belong to one of the student's groups.
	group, due_date, status = frappe.db.get_value(
		"MS Assignment", assignment, ["student_group", "due_date", "status"]
	)
	in_group = frappe.db.exists(
		"Student Group Student",
		{"parent": group, "parenttype": "Student Group", "student": student, "active": 1},
	)
	if not in_group:
		frappe.throw(_("This assignment is not for your class."), frappe.PermissionError)

	if status == "Closed":
		return fail(
			message_en="This assignment is closed for submissions.",
			message_ar="تم إغلاق هذا الواجب ولا يمكن التسليم.",
		)

	attachments = _normalise_files(files)
	if not (content or "").strip() and not attachments and not attachment:
		return fail(
			message_en="Write an answer or attach a file before submitting.",
			message_ar="يرجى كتابة إجابة أو إرفاق ملف قبل التسليم.",
		)

	# Past the due date the submission is flagged late rather than refused.
	new_status = "Late" if due_date and today() > str(due_date) else "Submitted"

	existing = frappe.db.get_value(
		"MS Assignment Submission", {"assignment": assignment, "student": student}, "name"
	)
	if existing:
		doc = frappe.get_doc("MS Assignment Submission", existing)
		if doc.status in ("Graded", "Returned"):
			return fail(
				message_en="This submission has already been graded.",
				message_ar="تم تصحيح هذا التسليم بالفعل.",
			)
		doc.content = content
		if attachment:
			doc.attachment = attachment
		doc.status = new_status
		doc.submitted_on = now_datetime()
		_replace_files(doc, attachments)
		doc.save()
		msg_en, msg_ar = "Submission updated.", "تم تحديث التسليم."
	else:
		doc = frappe.get_doc(
			{
				"doctype": "MS Assignment Submission",
				"assignment": assignment,
				"student": student,
				"content": content,
				"attachment": attachment,
				"status": new_status,
				"submitted_on": now_datetime(),
			}
		)
		_replace_files(doc, attachments)
		anchor_term(doc)
		doc.insert()
		msg_en, msg_ar = "Assignment submitted.", "تم تسليم الواجب."

	frappe.db.commit()
	return {
		"success": True,
		"data": {
			"id": doc.name,
			"status": doc.status,
			"status_label": SUBMISSION_STATUS_AR.get(doc.status, doc.status),
			"files": len(doc.files),
		},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}


def _normalise_files(files: str | list | None) -> list[dict]:
	"""Accept a JSON string or a list, and keep only rows that carry a URL."""
	if not files:
		return []
	rows = frappe.parse_json(files) if isinstance(files, str) else files
	if isinstance(rows, dict):
		rows = [rows]
	out = []
	for r in rows or []:
		if isinstance(r, str):
			out.append({"file_url": r, "file_name": r.rsplit("/", 1)[-1]})
		elif r.get("file_url"):
			out.append(
				{
					"file_url": r["file_url"],
					"file_name": r.get("file_name") or r["file_url"].rsplit("/", 1)[-1],
					"file_size": r.get("file_size") or 0,
				}
			)
	return out


def _replace_files(doc, attachments: list[dict]):
	"""Swap the attachment child table for the supplied list."""
	doc.set("files", [])
	for a in attachments:
		doc.append(
			"files",
			{
				"file_url": a["file_url"],
				"file_name": a.get("file_name"),
				"file_size": a.get("file_size") or 0,
				"uploaded_on": now_datetime(),
			},
		)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
		doc = frappe.get_doc("MS Assignment Submission", submission)
	else:
		existing = frappe.db.get_value(
			"MS Assignment Submission", {"assignment": assignment, "student": student}, "name"
		)
		if existing:
			doc = frappe.get_doc("MS Assignment Submission", existing)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "MS Assignment Submission",
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


# --- File uploads ----------------------------------------------------------

# Only these extensions may be attached, so a submission cannot be used to
# upload something executable.
ALLOWED_EXTENSIONS = {
	"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt",
	"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
	"zip", "rar", "7z",
	"mp3", "wav", "m4a", "mp4", "webm", "mov",
}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT)
def upload_file(persona: str = None):
	"""Store an uploaded file and return its URL for attaching.

	The browser posts multipart/form-data; Frappe puts it in frappe.request.files.
	"""
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(message_en="No file received.", message_ar="لم يتم استلام أي ملف.")

	filename = uploaded.filename or "file"
	extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	if extension not in ALLOWED_EXTENSIONS:
		return fail(
			message_en=f"File type .{extension} is not allowed.",
			message_ar=f"نوع الملف .{extension} غير مسموح به.",
		)

	content = uploaded.stream.read()
	if len(content) > MAX_UPLOAD_BYTES:
		return fail(
			message_en="File is larger than 15 MB.",
			message_ar="حجم الملف يتجاوز ١٥ ميجابايت.",
		)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			"is_private": 1,
			"attached_to_doctype": frappe.form_dict.get("doctype"),
			"attached_to_name": frappe.form_dict.get("docname"),
		}
	)
	file_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {
			"file_url": file_doc.file_url,
			"file_name": file_doc.file_name,
			"file_size": len(content),
		},
		"message_en": "File uploaded.",
		"message_ar": "تم رفع الملف.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT)
def delete_file(file_url: str, persona: str = None):
	"""Remove a previously uploaded file."""
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if name:
		frappe.delete_doc("File", name, ignore_permissions=True)
		frappe.db.commit()
	return {
		"success": True,
		"data": {"file_url": file_url},
		"message_en": "File removed.",
		"message_ar": "تم حذف الملف.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_submission(assignment: str, student: str = None, persona: str = None):
	"""One submission with its files — what the student sees when reopening."""
	scope = resolve_scope(persona)
	if persona == ROLE_STUDENT:
		student = scope.get("student")
	elif persona == ROLE_PARENT and not student:
		students = scope.get("students") or []
		student = students[0] if students else None

	if not student:
		return fail(message_en="Student not resolved.", message_ar="تعذّر تحديد الطالب.")

	assignment_doc = frappe.db.get_value(
		"MS Assignment",
		assignment,
		["name", "title", "description", "due_date", "maximum_score", "status", "course"],
		as_dict=True,
	)
	if not assignment_doc:
		return fail(message_en="Assignment not found.", message_ar="لم يتم العثور على الواجب.")

	submission_name = frappe.db.get_value(
		"MS Assignment Submission", {"assignment": assignment, "student": student}, "name"
	)
	submission = None
	if submission_name:
		doc = frappe.get_doc("MS Assignment Submission", submission_name)
		submission = {
			"id": doc.name,
			"status": doc.status,
			"status_label": SUBMISSION_STATUS_AR.get(doc.status, doc.status),
			"content": doc.content,
			"submitted_on": str(doc.submitted_on or ""),
			"score": flt(doc.score) if doc.score is not None else None,
			"feedback": doc.feedback,
			"files": [
				{
					"file_url": f.file_url,
					"file_name": f.file_name,
					"file_size": f.file_size,
				}
				for f in doc.files
			],
		}

	return {
		"assignment": {
			"id": assignment_doc.name,
			"title": assignment_doc.title,
			"description": assignment_doc.description,
			"due": str(assignment_doc.due_date or ""),
			"max": flt(assignment_doc.maximum_score),
			"status": assignment_doc.status,
			"course": assignment_doc.course,
			"files": [
				{"file_url": f.file_url, "file_name": f.file_name}
				for f in frappe.get_doc("MS Assignment", assignment).files
			],
		},
		"submission": submission,
	}
