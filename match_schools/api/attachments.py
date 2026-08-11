# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Documents attached to any record in the system.

A school runs on paper: birth certificates, ID copies, transfer letters,
medical notes, signed consent forms. This module lets any screen attach those
to the record they belong to — a student, a teacher, a guardian, an admission,
an enrolment, an invoice — without each screen inventing its own storage.

Files are stored as Frappe `File` documents against the parent record, which
means they also appear in the ERPNext desk, are covered by the site backup,
and are removed with the record they belong to.

Everything here is **private by default**. A student's ID copy must not be
readable by anyone who guesses the URL, so files are served through Frappe's
permission check rather than from the public folder. The one exception is a
profile photo, which has to render in an `<img>` and is handled by its own
endpoint elsewhere.
"""

import os

import frappe
from frappe import _
from frappe.utils import cint, flt

from match_schools.api.utils import (
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
)

# What a school actually needs to attach. Anything executable is refused:
# these files are downloaded by staff on school machines.
ALLOWED_EXTENSIONS = {
	# documents
	"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt",
	# images (scans and phone photos of paperwork)
	"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "heif", "tif", "tiff",
	# archives, for a bundle of scans
	"zip",
}

BLOCKED_EXTENSIONS = {
	"exe", "bat", "cmd", "com", "cpl", "dll", "msi", "scr", "vbs", "js", "jar",
	"sh", "ps1", "app", "apk", "php", "py", "pl", "html", "htm", "svg",
}

MAX_BYTES = 15 * 1024 * 1024  # 15 MB

# The record types a school attaches paperwork to. Restricting this is a
# security control, not tidiness: without it any authenticated user could
# attach to — and read attachments from — arbitrary doctypes such as User.
ATTACHABLE = {
	"Student": "الطالب",
	"Instructor": "المعلم",
	"Guardian": "ولي الأمر",
	"Student Applicant": "طلب الالتحاق",
	"Program Enrollment": "التسجيل",
	"Sales Invoice": "الفاتورة",
	"Student Group": "الشعبة",
	"MS Health Record": "الملف الصحي",
	"MS Behaviour Record": "السجل السلوكي",
	"MS Assignment": "الواجب",
	"Assessment Result": "نتيجة التقييم",
	"Course": "المادة",
	"Program": "الصف",
}


def _assert_attachable(doctype: str):
	if doctype not in ATTACHABLE:
		frappe.throw(
			_("لا يمكن إرفاق ملفات بهذا النوع من السجلات."), frappe.PermissionError
		)


def _student_of(doctype: str, name: str) -> str | None:
	"""The student a record belongs to, when it belongs to one.

	Used to decide whether a family may see the attachment: a parent should
	reach their child's enrolment documents, not another family's.
	"""
	if doctype == "Student":
		return name
	if doctype in ("Program Enrollment", "Sales Invoice", "MS Health Record",
	               "MS Behaviour Record", "Assessment Result"):
		return frappe.db.get_value(doctype, name, "student")
	if doctype == "Student Applicant":
		# An applicant is not yet a student; families cannot read these.
		return None
	return None


def _assert_can_read(persona: str, doctype: str, name: str):
	"""Who may see the documents on a record."""
	_assert_attachable(doctype)
	if persona in BACK_OFFICE:
		return

	student = _student_of(doctype, name)

	if persona == ROLE_STUDENT:
		if student and student == get_linked_student():
			return
	elif persona == ROLE_PARENT:
		if student and student in get_guardian_students(get_linked_guardian()):
			return
	elif persona == ROLE_TEACHER:
		instructor = get_linked_instructor()
		# A teacher's own record, and the teaching material they own.
		if doctype == "Instructor" and name == instructor:
			return
		if doctype in ("MS Assignment", "Course", "Program", "Student Group"):
			return
		if student and _teaches(instructor, student):
			return

	frappe.throw(_("لا تملك صلاحية الاطلاع على مرفقات هذا السجل."), frappe.PermissionError)


def _assert_can_write(persona: str, doctype: str, name: str):
	"""Who may add or remove documents.

	Deliberately narrower than reading: a parent may view their child's
	documents but not replace them, because these are the school's records of
	what was submitted.
	"""
	_assert_attachable(doctype)
	if persona in BACK_OFFICE:
		return

	if persona == ROLE_TEACHER:
		instructor = get_linked_instructor()
		if doctype == "Instructor" and name == instructor:
			return
		if doctype in ("MS Assignment", "Course", "MS Behaviour Record"):
			return

	frappe.throw(_("لا تملك صلاحية إضافة أو حذف مرفقات هذا السجل."), frappe.PermissionError)


def _teaches(instructor: str | None, student: str) -> bool:
	if not instructor:
		return False
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			  FROM `tabStudent Group Student` sgs
			  JOIN `tabStudent Group Instructor` sgi ON sgi.parent = sgs.parent
			 WHERE sgs.student = %(student)s AND sgi.instructor = %(instructor)s
			 LIMIT 1
			""",
			{"student": student, "instructor": instructor},
		)
	)


def _extension(filename: str) -> str:
	return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _human_size(size: int) -> str:
	value = flt(size)
	for unit in ("B", "KB", "MB"):
		if value < 1024:
			return f"{round(value, 1)} {unit}"
		value /= 1024
	return f"{round(value, 1)} GB"


def _row(f: dict) -> dict:
	return {
		"id": f.get("name"),
		"fileName": f.get("file_name"),
		"url": f.get("file_url"),
		"size": cint(f.get("file_size")),
		"sizeLabel": _human_size(cint(f.get("file_size"))),
		"isPrivate": bool(f.get("is_private")),
		"extension": _extension(f.get("file_name") or ""),
		"uploadedBy": f.get("owner"),
		"uploadedOn": str(f.get("creation") or ""),
	}


@frappe.whitelist()
@ms_endpoint()
def list_attachments(doctype: str = None, name: str = None, persona: str = None):
	"""Every document attached to one record."""
	if not doctype or not name:
		return fail(
			message_en="A record is required.",
			message_ar="يجب تحديد السجل.",
		)

	_assert_can_read(persona, doctype, name)

	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name},
		fields=[
			"name", "file_name", "file_url", "file_size", "is_private",
			"owner", "creation", "attached_to_field",
		],
		order_by="creation desc",
		limit_page_length=0,
	)

	# The profile photo is shown by the profile card itself; listing it again
	# among the documents is noise.
	rows = [r for r in rows if r.attached_to_field != "image"]

	can_write = True
	try:
		_assert_can_write(persona, doctype, name)
	except frappe.PermissionError:
		can_write = False

	return {
		"doctype": doctype,
		"name": name,
		"label": ATTACHABLE.get(doctype, doctype),
		"canWrite": can_write,
		"attachments": [_row(r) for r in rows],
		"total": len(rows),
		"maxBytes": MAX_BYTES,
		"allowed": sorted(ALLOWED_EXTENSIONS),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint()
def upload_attachment(doctype: str = None, name: str = None, persona: str = None):
	"""Attach an uploaded file to a record.

	Sent as multipart/form-data with `file`, because a base64 body would
	inflate a 15 MB scan to 20 MB over the wire.
	"""
	if not doctype or not name:
		return fail(
			message_en="A record is required.",
			message_ar="يجب تحديد السجل.",
		)

	_assert_can_write(persona, doctype, name)

	if not frappe.db.exists(doctype, name):
		return fail(
			message_en="That record no longer exists.",
			message_ar="هذا السجل لم يعد موجوداً.",
		)

	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(
			message_en="No file received.",
			message_ar="لم يتم استلام أي ملف.",
		)

	filename = os.path.basename(uploaded.filename or "attachment")
	extension = _extension(filename)

	if not extension:
		return fail(
			message_en="The file has no extension.",
			message_ar="الملف بدون امتداد معروف.",
		)
	if extension in BLOCKED_EXTENSIONS:
		return fail(
			message_en=f"Files of type .{extension} are not allowed.",
			message_ar=f"لا يُسمح برفع ملفات من نوع .{extension} لأسباب أمنية.",
		)
	if extension not in ALLOWED_EXTENSIONS:
		return fail(
			message_en=f"Files of type .{extension} are not supported.",
			message_ar=f"نوع الملف .{extension} غير مدعوم.",
		)

	content = uploaded.stream.read()
	if not content:
		return fail(
			message_en="The file is empty.",
			message_ar="الملف فارغ.",
		)
	if len(content) > MAX_BYTES:
		return fail(
			message_en="The file is larger than 15 MB.",
			message_ar="حجم الملف يتجاوز ١٥ ميجابايت.",
		)

	description = (frappe.form_dict.get("description") or "").strip()

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			# Private: school paperwork is served behind a permission check
			# rather than from a guessable public URL.
			"is_private": 1,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
		}
	)
	file_doc.insert(ignore_permissions=True)

	if description:
		# Frappe's File has no description field; the note is kept as a comment
		# on the parent so it survives with the record.
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Attachment",
				"reference_doctype": doctype,
				"reference_name": name,
				"content": f"{filename} — {description}",
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()

	return {
		"attachment": _row(
			{
				"name": file_doc.name,
				"file_name": file_doc.file_name,
				"file_url": file_doc.file_url,
				"file_size": file_doc.file_size,
				"is_private": file_doc.is_private,
				"owner": file_doc.owner,
				"creation": file_doc.creation,
			}
		),
		"message_ar": f"تم رفع {filename}.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint()
def delete_attachment(attachment: str = None, persona: str = None):
	"""Remove a document from a record."""
	if not attachment:
		return fail(
			message_en="An attachment is required.",
			message_ar="يجب تحديد المرفق.",
		)

	file_doc = frappe.db.get_value(
		"File",
		attachment,
		["name", "file_name", "attached_to_doctype", "attached_to_name", "attached_to_field"],
		as_dict=True,
	)
	if not file_doc:
		return fail(
			message_en="That attachment no longer exists.",
			message_ar="هذا المرفق لم يعد موجوداً.",
		)

	# Permission comes from the record it hangs on, not from the file.
	_assert_can_write(persona, file_doc.attached_to_doctype, file_doc.attached_to_name)

	if file_doc.attached_to_field == "image":
		return fail(
			message_en="Use the profile photo control to change the picture.",
			message_ar="لتغيير الصورة الشخصية استخدم أداة الصورة في الملف.",
		)

	frappe.delete_doc("File", attachment, ignore_permissions=True)
	frappe.db.commit()

	return {"message_ar": f"تم حذف {file_doc.file_name}."}


@frappe.whitelist()
@ms_endpoint()
def attachment_counts(doctype: str = None, names: str | list = None, persona: str = None):
	"""How many documents each record carries, for a list column.

	One query for the whole page rather than one per row.
	"""
	from match_schools.api.utils import parse_json_arg

	ids = parse_json_arg(names, []) or []
	if isinstance(ids, str):
		ids = [ids]
	if not doctype or not ids:
		return {}

	_assert_attachable(doctype)

	# Raw SQL: the query builder refuses an aggregate written as a string, and
	# a per-row count would be one query per list row.
	rows = frappe.db.sql(
		"""
		SELECT attached_to_name, COUNT(name) AS total
		  FROM `tabFile`
		 WHERE attached_to_doctype = %(doctype)s
		   AND attached_to_name IN %(names)s
		   AND IFNULL(attached_to_field, '') != 'image'
		 GROUP BY attached_to_name
		""",
		{"doctype": doctype, "names": tuple(ids)},
		as_dict=True,
	)
	return {r.attached_to_name: cint(r.total) for r in rows}
