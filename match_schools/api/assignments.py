# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Assignments and student submissions."""

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now, now_datetime, today

from match_schools.api.utils import (
	apply_period,
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
		# A draft is the teacher's working copy and a scheduled one has not
		# arrived yet. Neither is homework as far as a family is concerned.
		filters["is_published"] = 1

	if student_group:
		filters["student_group"] = student_group
	if course:
		filters["course"] = course
	if status:
		filters["status"] = status

	# Without this every year's homework arrives in one list and the header's
	# period switcher does nothing on this screen.
	apply_period(filters, "MS Assignment")

	# A piece of homework set for several sections is listed by any of them,
	# not only by the one that happens to be stored on the parent record.
	if student_group:
		names = set(
			frappe.get_all("MS Assignment", filters=filters, pluck="name")
		) | set(
			frappe.get_all(
				"MS Assignment Group",
				filters={"student_group": student_group, "parenttype": "MS Assignment"},
				pluck="parent",
			)
		)
		reach = {**filters}
		reach.pop("student_group", None)
		allowed_names = set(frappe.get_all("MS Assignment", filters=reach, pluck="name"))
		filters = {"name": ["in", sorted(names & allowed_names) or [""]]}

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

	# The same worksheet usually goes to every section a teacher takes. The
	# original single field stays as the first of them so older records and
	# screens keep working unchanged.
	groups = [g for g in (data.get("student_groups") or []) if g]
	primary = data.get("student_group") or (groups[0] if groups else None)
	if primary and primary not in groups:
		groups.insert(0, primary)

	is_draft = cint(data.get("is_draft"))
	publish_at = (data.get("publish_at") or "").strip() or None
	if publish_at and get_datetime(publish_at) <= get_datetime(now()):
		# A publish time in the past is a publish now, not an error.
		publish_at = None

	fields = {
		"title": data.get("title"),
		"course": data.get("course"),
		"student_group": primary,
		"due_date": data.get("due_date"),
		"assigned_on": data.get("assigned_on") or today(),
		"maximum_score": data.get("maximum_score") or 100,
		"status": data.get("status") or "Open",
		"description": data.get("description"),
		"objectives": data.get("objectives"),
		"requirements": data.get("requirements"),
		"submission_type": data.get("submission_type") or "رفع ملف",
		"allow_late": 1 if cint(data.get("allow_late", 1)) else 0,
		"allow_questions": 1 if cint(data.get("allow_questions", 1)) else 0,
		"notify_guardians": 1 if cint(data.get("notify_guardians", 1)) else 0,
		"is_draft": 1 if is_draft else 0,
		"publish_at": publish_at if not is_draft else None,
		# Draft or waiting for its time: not visible to anyone but its author.
		"is_published": 0 if (is_draft or publish_at) else 1,
		"solution_body": data.get("solution_body"),
		"solution_published": 1 if cint(data.get("solution_published")) else 0,
	}
	fields = {k: v for k, v in fields.items() if v is not None}

	attachments = _normalise_files(data.get("files"))

	assignment_id = data.get("id") or data.get("name")
	if assignment_id:
		doc = frappe.get_doc("MS Assignment", assignment_id)
		_assert_owns_assignment(doc, persona, scope)
		was_published = cint(doc.is_published)
		doc.update(fields)
		if data.get("files") is not None:
			_replace_files(doc, attachments)
		_apply_groups(doc, groups)
		_apply_links(doc, data.get("links"))
		_apply_solution_files(doc, data)
		_stamp_publish(doc, was_published)
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
		_apply_groups(doc, groups)
		_apply_links(doc, data.get("links"))
		_apply_solution_files(doc, data)
		_stamp_publish(doc, 0)
		doc.insert()
		msg_en, msg_ar = (
			("Draft saved.", "تم حفظ المسودة.")
			if is_draft
			else ("Assignment scheduled.", f"سيُنشر الواجب في {str(publish_at)[:16]}.")
			if publish_at
			else ("Assignment created.", "تم إنشاء الواجب.")
		)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.title},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}



def _apply_groups(doc, groups: list[str]) -> None:
	"""Set the classes a piece of homework goes to."""
	if not groups:
		return
	doc.set("groups", [])
	for group in dict.fromkeys(groups):
		doc.append(
			"groups",
			{
				"student_group": group,
				"student_group_name": frappe.db.get_value(
					"Student Group", group, "student_group_name"
				),
			},
		)


def _apply_links(doc, links) -> None:
	"""Replace the links that go out with the homework."""
	if links is None:
		return
	doc.set("links", [])
	for row in links or []:
		url = ((row or {}).get("url") or "").strip()
		if not url:
			continue
		doc.append(
			"links",
			{
				"title": (row.get("title") or url)[:140],
				"url": url[:500],
				"kind": row.get("kind") or "رابط",
			},
		)


def _apply_solution_files(doc, data: dict) -> None:
	if data.get("solution_files") is None:
		return
	doc.set("solution_files", [])
	for f in _normalise_files(data.get("solution_files")):
		doc.append("solution_files", f)


def _stamp_publish(doc, was_published: int) -> None:
	"""Record when the homework — and its answers — actually became visible."""
	if cint(doc.is_published) and not was_published:
		doc.published_on = now()
	if cint(doc.solution_published) and not doc.solution_published_on:
		doc.solution_published_on = now()
	if not cint(doc.solution_published):
		doc.solution_published_on = None


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


# ---------------------------------------------------------------------------
# The grading grid
# ---------------------------------------------------------------------------


def _target_groups(doc) -> list[str]:
	"""Every class this homework was set for."""
	groups = [g.student_group for g in (doc.get("groups") or []) if g.student_group]
	if doc.student_group and doc.student_group not in groups:
		groups.insert(0, doc.student_group)
	return groups


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def grading_sheet(assignment: str = None, student_group: str = None, persona: str = None):
	"""Every pupil the homework was set for, one row each.

	The same shape as mark entry, because that is the job: a teacher marking
	thirty pieces of work should type down a column, not open thirty dialogs.

	A row exists for every pupil, not only those who handed something in —
	"has not submitted" is the most important state on this sheet and it has
	no record of its own to be listed from.
	"""
	if not assignment:
		return fail(message_en="An assignment is required.", message_ar="يجب تحديد الواجب.")

	doc = frappe.get_doc("MS Assignment", assignment)
	_assert_owns_assignment(doc, persona, resolve_scope(persona))

	groups = [student_group] if student_group else _target_groups(doc)
	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": ["in", groups or [""]], "active": 1},
		fields=["student", "student_name", "parent", "group_roll_number"],
		order_by="parent, group_roll_number, student_name",
		limit_page_length=0,
	)
	if not roster:
		return {"assignment": _assignment_row(doc), "students": [], "groups": groups}

	students = [r.student for r in roster]
	submissions = {
		s.student: s
		for s in frappe.get_all(
			"MS Assignment Submission",
			filters={"assignment": assignment, "student": ["in", students]},
			fields=[
				"name", "student", "status", "submitted_on", "viewed_on",
				"guardian_viewed_on", "score", "maximum_score", "feedback",
				"teacher_note", "is_late", "content", "graded_on",
			],
			limit_page_length=0,
		)
	}

	files: dict[str, list] = {}
	if submissions:
		for f in frappe.get_all(
			"MS Attachment",
			filters={
				"parent": ["in", [s.name for s in submissions.values()]],
				"parenttype": "MS Assignment Submission",
			},
			fields=["parent", "file_url", "file_name", "file_size"],
			limit_page_length=0,
		):
			files.setdefault(f.parent, []).append(
				{"file_url": f.file_url, "file_name": f.file_name, "file_size": f.file_size}
			)

	group_names = {
		g.name: g.student_group_name
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", groups or [""]]},
			fields=["name", "student_group_name"],
		)
	}

	rows = []
	for r in roster:
		sub = submissions.get(r.student)
		rows.append(
			{
				"student": r.student,
				"name": r.student_name,
				"roll": cint(r.group_roll_number),
				"student_group": r.parent,
				"group_name": group_names.get(r.parent) or r.parent,
				"submission": sub.name if sub else None,
				# "لم يُسلّم" is a state of the pupil, not of a missing row.
				"status": _state(sub),
				"submitted_on": str(sub.submitted_on or "") if sub else "",
				"viewed_on": str(sub.viewed_on or "") if sub else "",
				"guardian_viewed_on": str(sub.guardian_viewed_on or "") if sub else "",
				"is_late": bool(cint(sub.is_late)) if sub else False,
				# Frappe writes a Float as 0, not NULL, so an ungraded row would
				# otherwise read as a zero the teacher never gave.
				"score": flt(sub.score) if (sub and sub.graded_on) else None,
				"max_score": flt(sub.maximum_score) if sub else flt(doc.maximum_score),
				"feedback": (sub.feedback if sub else None),
				"teacher_note": (sub.teacher_note if sub else None),
				"content": (sub.content if sub else None),
				"graded_on": str(sub.graded_on or "") if sub else "",
				"files": files.get(sub.name, []) if sub else [],
			}
		)

	submitted = sum(1 for r in rows if r["submitted_on"])
	return {
		"assignment": _assignment_row(doc),
		"groups": [{"id": g, "name": group_names.get(g) or g} for g in groups],
		"students": rows,
		"summary": {
			"total": len(rows),
			"submitted": submitted,
			"missing": len(rows) - submitted,
			"graded": sum(1 for r in rows if r["graded_on"]),
			"late": sum(1 for r in rows if r["is_late"]),
			"seen": sum(1 for r in rows if r["viewed_on"]),
		},
	}


# What the marking sheet shows in the status column. Read from the record
# rather than stored, so a pupil who opened the work and did nothing reads as
# "اطّلع ولم يُسلّم" instead of the doctype's English default.
STATE_AR = {
	"Submitted": "سُلّم",
	"Late": "سُلّم متأخراً",
	"Graded": "مُصحّح",
	"Returned": "أُعيد للطالب",
	"Viewed": "اطّلع ولم يُسلّم",
	"Pending": "لم يُسلّم",
}


def _state(sub) -> str:
	if not sub:
		return "لم يُسلّم"
	if sub.status in ("Graded", "Returned"):
		return STATE_AR[sub.status]
	if sub.submitted_on:
		return STATE_AR["Late"] if cint(sub.is_late) else STATE_AR["Submitted"]
	if sub.viewed_on:
		return STATE_AR["Viewed"]
	return STATE_AR["Pending"]


def _assignment_row(doc) -> dict:
	return {
		"id": doc.name,
		"title": doc.title,
		"course": doc.course,
		"student_group": doc.student_group,
		"groups": [g.student_group for g in (doc.get("groups") or [])],
		"due_date": str(doc.due_date or ""),
		"assigned_on": str(doc.assigned_on or ""),
		"maximum_score": flt(doc.maximum_score),
		"status": doc.status,
		"description": doc.description,
		"objectives": doc.get("objectives"),
		"requirements": doc.get("requirements"),
		"submission_type": doc.get("submission_type"),
		"allow_late": bool(cint(doc.get("allow_late"))),
		"allow_questions": bool(cint(doc.get("allow_questions"))),
		"is_draft": bool(cint(doc.get("is_draft"))),
		"is_published": bool(cint(doc.get("is_published"))),
		"publish_at": str(doc.get("publish_at") or ""),
		"solution_published": bool(cint(doc.get("solution_published"))),
		"links": [
			{"title": l.title, "url": l.url, "kind": l.kind} for l in (doc.get("links") or [])
		],
		"files": [
			{"file_url": f.file_url, "file_name": f.file_name, "file_size": f.file_size}
			for f in (doc.get("files") or [])
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_grades(payload: str | dict = None, persona: str = None):
	"""Save a column of marks and notes in one go."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	assignment = data.get("assignment")
	rows = data.get("rows") or {}
	if not assignment:
		return fail(message_en="An assignment is required.", message_ar="يجب تحديد الواجب.")

	doc = frappe.get_doc("MS Assignment", assignment)
	_assert_owns_assignment(doc, persona, resolve_scope(persona))

	# Only pupils the homework was actually set for: an id that arrives in the
	# payload but is not on the roster must not get a mark.
	roster = set(
		frappe.get_all(
			"Student Group Student",
			filters={"parent": ["in", _target_groups(doc) or [""]], "active": 1},
			pluck="student",
		)
	)
	maximum = flt(doc.maximum_score) or 100
	saved = 0

	for student, row in rows.items():
		if student not in roster:
			continue
		row = row or {}
		score = row.get("score")
		note = row.get("teacher_note")
		feedback = row.get("feedback")
		status = row.get("status")
		if score in (None, "") and note is None and feedback is None and not status:
			continue

		existing = frappe.get_all(
			"MS Assignment Submission",
			filters={"assignment": assignment, "student": student},
			pluck="name",
			limit=1,
		)
		if existing:
			sub = frappe.get_doc("MS Assignment Submission", existing[0])
		else:
			# Marking work handed in on paper: the record is created here so
			# the mark has somewhere to live.
			sub = frappe.new_doc("MS Assignment Submission")
			sub.assignment = assignment
			sub.assignment_title = doc.title
			sub.student = student
			sub.student_name = frappe.db.get_value("Student", student, "student_name")
			sub.status = "Pending"

		if score not in (None, ""):
			value = flt(score)
			if value < 0 or value > maximum:
				return fail(
					message_en=f"A mark must be between 0 and {maximum}.",
					message_ar=f"العلامة يجب أن تكون بين صفر و{maximum:g}.",
				)
			sub.score = value
			sub.maximum_score = maximum
			sub.graded_by = frappe.session.user
			sub.graded_on = now()
			sub.status = "Graded"
		if note is not None:
			sub.teacher_note = note
		if feedback is not None:
			sub.feedback = feedback
		if status:
			sub.status = status

		sub.save(ignore_permissions=True)
		saved += 1

	frappe.db.commit()
	return {
		"success": True,
		"data": {"saved": saved},
		"message_en": f"Saved {saved} row(s).",
		"message_ar": f"تم حفظ {saved} صفاً.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_STUDENT, ROLE_PARENT)
def record_view(assignment: str = None, student: str = None, persona: str = None):
	"""Note that a pupil — or their guardian — has opened the homework.

	This is what lets a teacher tell "has not seen it" from "seen it and not
	done it", which are two very different conversations. Only the first view
	is kept: a timestamp that moves every time the page is opened says nothing.
	"""
	if not assignment:
		return fail(message_en="An assignment is required.", message_ar="يجب تحديد الواجب.")

	scope = resolve_scope(persona)
	students = scope.get("students") or []
	target = student or (students[0] if students else None)
	if not target or target not in students:
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	field = "viewed_on" if persona == ROLE_STUDENT else "guardian_viewed_on"
	existing = frappe.get_all(
		"MS Assignment Submission",
		filters={"assignment": assignment, "student": target},
		fields=["name", field],
		limit=1,
	)
	if existing:
		if not existing[0].get(field):
			frappe.db.set_value(
				"MS Assignment Submission", existing[0].name, field, now(), update_modified=False
			)
	else:
		doc = frappe.new_doc("MS Assignment Submission")
		doc.assignment = assignment
		doc.assignment_title = frappe.db.get_value("MS Assignment", assignment, "title")
		doc.student = target
		doc.student_name = frappe.db.get_value("Student", target, "student_name")
		doc.status = "Viewed"
		doc.set(field, now())
		doc.insert(ignore_permissions=True)

	frappe.db.commit()
	return {"success": True, "data": {}, "message_en": "", "message_ar": ""}


# ---------------------------------------------------------------------------
# Questions about the homework
# ---------------------------------------------------------------------------


def _may_see_assignment(doc, persona: str, scope: dict) -> bool:
	if persona in (ROLE_ADMIN, ROLE_SECRETARY):
		return True
	if persona == ROLE_TEACHER:
		return not doc.instructor or doc.instructor == scope.get("instructor")
	if not cint(doc.get("is_published")):
		return False
	mine = _groups_for_students(scope.get("students") or [])
	return bool(set(_target_groups(doc)) & set(mine))


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def questions(assignment: str = None, persona: str = None):
	"""Questions asked about one piece of homework.

	A pupil sees their own and any the teacher marked public — the next pupil
	stuck on the same step should be able to read the answer instead of asking
	it again.
	"""
	if not assignment:
		return fail(message_en="An assignment is required.", message_ar="يجب تحديد الواجب.")

	doc = frappe.get_doc("MS Assignment", assignment)
	scope = resolve_scope(persona)
	if not _may_see_assignment(doc, persona, scope):
		frappe.throw(_("This assignment is not yours."), frappe.PermissionError)

	rows = frappe.get_all(
		"MS Assignment Question",
		filters={"assignment": assignment},
		fields=[
			"name", "student", "student_name", "asked_by", "asked_on", "body",
			"answer", "answered_by", "answered_on", "is_public",
		],
		order_by="asked_on desc",
		limit_page_length=100,
	)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		mine = set(scope.get("students") or [])
		rows = [r for r in rows if r.student in mine or cint(r.is_public)]

	return {
		"questions": [
			{
				"id": r.name,
				"student": r.student,
				"student_name": r.student_name,
				"body": r.body,
				"asked_on": str(r.asked_on or ""),
				"answer": r.answer,
				"answered_on": str(r.answered_on or ""),
				"is_public": bool(cint(r.is_public)),
				"mine": r.asked_by == frappe.session.user,
				"answered": bool(r.answer),
			}
			for r in rows
		],
		"unanswered": sum(1 for r in rows if not r.answer),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_STUDENT, ROLE_PARENT)
def ask_question(
	assignment: str = None, body: str = None, student: str = None, persona: str = None
):
	"""Ask the teacher about a piece of homework."""
	body = (body or "").strip()
	if not (assignment and body):
		return fail(message_en="Write your question.", message_ar="اكتب نص الاستفسار.")
	if len(body) > 2000:
		return fail(message_en="The question is too long.", message_ar="الاستفسار طويل جداً.")

	doc = frappe.get_doc("MS Assignment", assignment)
	scope = resolve_scope(persona)
	if not _may_see_assignment(doc, persona, scope):
		frappe.throw(_("This assignment is not yours."), frappe.PermissionError)
	if not cint(doc.get("allow_questions")):
		return fail(
			message_en="This assignment does not take questions.",
			message_ar="هذا الواجب لا يستقبل استفسارات.",
		)

	students = scope.get("students") or []
	target = student or (students[0] if students else None)
	if not target or target not in students:
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	# One open question at a time per pupil: a queue of nine from the same
	# child is a conversation, and the teacher answers the first anyway.
	if frappe.db.exists(
		"MS Assignment Question",
		{"assignment": assignment, "student": target, "answer": ["in", ["", None]]},
	):
		return fail(
			message_en="You already have a question awaiting an answer.",
			message_ar="لديك استفسار بانتظار رد المعلم على هذا الواجب.",
		)

	q = frappe.get_doc(
		{
			"doctype": "MS Assignment Question",
			"assignment": assignment,
			"assignment_title": doc.title,
			"student": target,
			"student_name": frappe.db.get_value("Student", target, "student_name"),
			"asked_by": frappe.session.user,
			"asked_on": now(),
			"body": body,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": q.name},
		"message_en": "Question sent to the teacher.",
		"message_ar": "تم إرسال الاستفسار إلى المعلم.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def answer_question(
	question: str = None, answer: str = None, is_public: int = 0, persona: str = None
):
	"""Answer a pupil's question."""
	answer = (answer or "").strip()
	if not (question and answer):
		return fail(message_en="Write your answer.", message_ar="اكتب نص الرد.")

	q = frappe.get_doc("MS Assignment Question", question)
	doc = frappe.get_doc("MS Assignment", q.assignment)
	_assert_owns_assignment(doc, persona, resolve_scope(persona))

	q.answer = answer[:2000]
	q.answered_by = frappe.session.user
	q.answered_on = now()
	q.is_public = 1 if cint(is_public) else 0
	q.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": q.name},
		"message_en": "Answer sent.",
		"message_ar": "تم إرسال الرد على الاستفسار.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def solution(assignment: str = None, persona: str = None):
	"""حلول الواجب — the model answer, once the teacher releases it."""
	if not assignment:
		return fail(message_en="An assignment is required.", message_ar="يجب تحديد الواجب.")

	doc = frappe.get_doc("MS Assignment", assignment)
	scope = resolve_scope(persona)
	if not _may_see_assignment(doc, persona, scope):
		frappe.throw(_("This assignment is not yours."), frappe.PermissionError)

	# Released or not, a family must not be able to read the answers early by
	# asking for them directly.
	released = bool(cint(doc.get("solution_published")))
	if persona in (ROLE_STUDENT, ROLE_PARENT) and not released:
		return {"published": False, "body": None, "files": []}

	return {
		"published": released,
		"published_on": str(doc.get("solution_published_on") or ""),
		"body": doc.get("solution_body"),
		"files": [
			{"file_url": f.file_url, "file_name": f.file_name, "file_size": f.file_size}
			for f in (doc.get("solution_files") or [])
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def publish_solution(assignment: str = None, published: int = 1, persona: str = None):
	"""Show or hide the model answer."""
	if not assignment:
		return fail(message_en="An assignment is required.", message_ar="يجب تحديد الواجب.")

	doc = frappe.get_doc("MS Assignment", assignment)
	_assert_owns_assignment(doc, persona, resolve_scope(persona))

	doc.solution_published = 1 if cint(published) else 0
	doc.solution_published_on = now() if doc.solution_published else None
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "published": bool(doc.solution_published)},
		"message_en": "Solution visibility updated.",
		"message_ar": (
			"أصبح الحل ظاهراً للطلاب وأولياء الأمور."
			if doc.solution_published
			else "تم إخفاء الحل."
		),
	}


def publish_due_assignments():
	"""Release homework whose publish time has arrived.

	Runs on the scheduler beside the mail queue, for the same reason: a
	teacher who prepared work at midnight to appear on Sunday morning should
	not have to be awake to press a button.
	"""
	due = frappe.get_all(
		"MS Assignment",
		filters={
			"is_published": 0,
			"is_draft": 0,
			"publish_at": ["<=", now()],
		},
		pluck="name",
		limit_page_length=200,
	)
	for name in due:
		try:
			frappe.db.set_value(
				"MS Assignment",
				name,
				{"is_published": 1, "published_on": now()},
				update_modified=False,
			)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="تعذّر نشر واجب مجدول", message=f"{name}\n{frappe.get_traceback()}"
			)
