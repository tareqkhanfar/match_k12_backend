"""Teachers send work to the office to be printed, and can see where it is.

Printing an exam means walking to the office, explaining what is needed, and
then coming back later to ask whether it is done. The request itself was never
written down, so nothing could be looked up: how many copies, which class,
whether it was ever printed.

A request carries the files, the instructions and a status the office moves
along. The teacher sees the status without asking, and the office sees a queue
instead of a pile of verbal requests.

The status is a one-way street by design — Submitted, In Progress, Ready,
Collected — with Rejected as the exit when the office cannot do it. Moving
backwards would let a collected job silently become pending again, and the
teacher would have no way to know their exam went missing from the queue.
"""

import frappe
from frappe import _
from frappe.utils import cint, now

from match_schools.api.utils import (
	FRAPPE_ROLE_BY_PERSONA,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)

# What a school actually prints. Deliberately narrower than the general
# uploader: an exam is a document, and a 200 MB video in the print queue is a
# mistake nobody wants to discover at the printer.
ALLOWED_EXTENSIONS = {
	"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
	"txt", "rtf", "odt", "png", "jpg", "jpeg",
}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 10

STATUS_AR = {
	"Submitted": "بانتظار الطباعة",
	"In Progress": "قيد الطباعة",
	"Ready": "جاهز للاستلام",
	"Collected": "تم الاستلام",
	"Rejected": "مرفوض",
}
STATUS_TONE = {
	"Submitted": "muted",
	"In Progress": "warning",
	"Ready": "success",
	"Collected": "muted",
	"Rejected": "danger",
}
TYPE_AR = {
	"Exam": "امتحان",
	"Worksheet": "ورقة عمل",
	"Homework": "واجب",
	"Handout": "نشرة",
	"Other": "أخرى",
}

# Which moves the office may make from each state. A collected job is finished;
# reopening it would lose the record of what actually happened.
NEXT_STATUS = {
	"Submitted": {"In Progress", "Ready", "Rejected"},
	"In Progress": {"Ready", "Rejected"},
	"Ready": {"Collected", "In Progress"},
	"Collected": set(),
	"Rejected": set(),
}


def _may_edit(persona: str, doc) -> bool:
	"""The teacher who raised it, while the office has not started."""
	if persona in BACK_OFFICE:
		return True
	if doc.requested_by != frappe.session.user:
		return False
	# Once the office has picked it up, editing the files underneath them would
	# mean printing something the teacher can no longer see.
	return doc.status == "Submitted"


def _row(doc, persona: str) -> dict:
	return {
		"id": doc.name,
		"title": doc.title,
		"document_type": doc.document_type,
		"type_label": TYPE_AR.get(doc.document_type, doc.document_type),
		"status": doc.status,
		"status_label": STATUS_AR.get(doc.status, doc.status),
		"status_tone": STATUS_TONE.get(doc.status, "muted"),
		"priority": doc.priority,
		"urgent": doc.priority == "Urgent",
		"needed_by": str(doc.needed_by or ""),
		"student_group": doc.student_group,
		"course": doc.course,
		"copies": cint(doc.copies),
		"notes": doc.notes,
		"secretary_notes": doc.secretary_notes,
		"requested_by": doc.requested_by,
		"requested_by_name": frappe.db.get_value("User", doc.requested_by, "full_name"),
		"requested_on": str(doc.requested_on or ""),
		"handled_by_name": (
			frappe.db.get_value("User", doc.handled_by, "full_name") if doc.handled_by else None
		),
		"completed_on": str(doc.completed_on or ""),
		"can_edit": _may_edit(persona, doc),
		"can_handle": persona in BACK_OFFICE,
		"attachments": [
			{
				"file_url": a.file_url,
				"file_name": a.file_name,
				"file_size": cint(a.file_size),
				"pages": cint(a.pages),
			}
			for a in (doc.attachments or [])
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def list_requests(status: str = None, mine: int = 0, persona: str = None):
	"""The print queue, or a teacher's own requests.

	A teacher sees only what they sent — another teacher's exam paper is not
	theirs to read, and the files are attached to the request.
	"""
	filters: dict = {}
	if persona == ROLE_TEACHER or cint(mine):
		filters["requested_by"] = frappe.session.user
	if status and status != "all":
		filters["status"] = status

	names = frappe.get_all(
		"MS Print Request",
		filters=filters,
		pluck="name",
		order_by="requested_on asc",
		limit_page_length=200,
	)
	rows = [_row(frappe.get_doc("MS Print Request", n), persona) for n in names]
	# Urgent first, then oldest: the office works a queue, not a stack. Sorted
	# here because Frappe's query builder rejects SQL functions in order_by.
	rows.sort(key=lambda r: (not r["urgent"], r["requested_on"]))

	# Counted per status for the tabs. Frappe's builder rejects SQL functions
	# written as strings, so this counts rows rather than aggregating in SQL —
	# a print queue is tens of rows, not thousands.
	counts: dict[str, int] = {}
	scope_filter = (
		{"requested_by": frappe.session.user} if persona == ROLE_TEACHER else {}
	)
	for status_key in STATUS_AR:
		counts[status_key] = frappe.db.count(
			"MS Print Request", {**scope_filter, "status": status_key}
		)

	return {
		"requests": rows,
		"counts": counts,
		"statuses": [
			{"value": k, "label": v, "tone": STATUS_TONE[k]} for k, v in STATUS_AR.items()
		],
		"types": [{"value": k, "label": v} for k, v in TYPE_AR.items()],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_request(request: str = None, persona: str = None):
	if not request:
		return fail(message_en="A request is required.", message_ar="يجب تحديد الطلب.")
	doc = frappe.get_doc("MS Print Request", request)
	if persona == ROLE_TEACHER and doc.requested_by != frappe.session.user:
		frappe.throw(_("This request is not yours."), frappe.PermissionError)
	return _row(doc, persona)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_request(payload: str | dict = None, persona: str = None):
	"""Create or update a print request."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	request = data.get("request")

	if request:
		doc = frappe.get_doc("MS Print Request", request)
		if not _may_edit(persona, doc):
			frappe.throw(
				_("This request can no longer be changed."), frappe.PermissionError
			)
	else:
		doc = frappe.new_doc("MS Print Request")
		doc.requested_by = frappe.session.user
		doc.requested_on = now()
		doc.status = "Submitted"

	for field in ("title", "document_type", "priority", "needed_by",
	              "student_group", "course", "notes"):
		if field in data:
			doc.set(field, data.get(field))
	if "copies" in data:
		doc.copies = max(cint(data.get("copies")), 1)

	if not (doc.title or "").strip():
		return fail(message_en="A title is required.", message_ar="عنوان الطلب مطلوب.")

	files = data.get("attachments")
	if files is not None:
		if len(files) > MAX_FILES:
			return fail(
				message_en=f"At most {MAX_FILES} files per request.",
				message_ar=f"الحد الأقصى {MAX_FILES} ملفات للطلب الواحد.",
			)
		doc.set("attachments", [])
		for f in files:
			url = (f or {}).get("file_url")
			if not url:
				continue
			doc.append(
				"attachments",
				{
					"file_url": url,
					"file_name": f.get("file_name"),
					"file_size": cint(f.get("file_size")),
					"pages": cint(f.get("pages")),
				},
			)

	# Anchored like everything else: a request belongs to the term it was made
	# in, or the queue is unreadable a year later.
	if not doc.academic_year:
		doc.academic_year = get_default_academic_year()
	if not doc.academic_term:
		doc.academic_term = get_default_academic_term()

	is_new = doc.is_new()
	doc.save(ignore_permissions=True)
	_link_files(doc)
	if is_new:
		_notify_office(doc)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "status": doc.status},
		"message_en": "Print request saved.",
		"message_ar": "تم حفظ طلب الطباعة.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def set_status(
	request: str = None, status: str = None, notes: str = None, persona: str = None
):
	"""The office moves a request along.

	Only forward, and only along a defined path: a collected job cannot become
	pending again, which would hide from the teacher that their exam left the
	queue.
	"""
	if not request or not status:
		return fail(
			message_en="A request and a status are required.",
			message_ar="يجب تحديد الطلب والحالة.",
		)
	if status not in STATUS_AR:
		return fail(message_en="Unknown status.", message_ar="حالة غير معروفة.")

	doc = frappe.get_doc("MS Print Request", request)
	if status != doc.status and status not in NEXT_STATUS.get(doc.status, set()):
		return fail(
			message_en=f"Cannot move from {doc.status} to {status}.",
			message_ar=(
				f"لا يمكن الانتقال من «{STATUS_AR[doc.status]}» "
				f"إلى «{STATUS_AR[status]}»."
			),
		)

	doc.status = status
	doc.handled_by = frappe.session.user
	if notes is not None:
		doc.secretary_notes = notes
	if status in ("Ready", "Collected", "Rejected"):
		doc.completed_on = now()
	doc.save(ignore_permissions=True)
	if status in ("Ready", "Rejected") and doc.requested_by:
		from match_schools import push

		push.notify(
			[doc.requested_by],
			"طلب الطباعة جاهز" if status == "Ready" else "رُفض طلب الطباعة",
			doc.title + (f" — {notes}" if notes else ""),
			channel="general",
			link="/app/print-requests",
		)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "status": status, "label": STATUS_AR[status]},
		"message_en": f"Marked as {status}.",
		"message_ar": f"تم التحديث إلى «{STATUS_AR[status]}».",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_request(request: str = None, persona: str = None):
	"""Withdraw a request the office has not started on."""
	if not request:
		return fail(message_en="A request is required.", message_ar="يجب تحديد الطلب.")
	doc = frappe.get_doc("MS Print Request", request)
	if not _may_edit(persona, doc):
		frappe.throw(
			_("This request can no longer be withdrawn."), frappe.PermissionError
		)

	# The files were uploaded for this request alone; leaving them behind is
	# storage nobody can find again. A file that will not delete (already gone
	# from disk) must not keep the request itself alive.
	for a in doc.attachments or []:
		for name in frappe.get_all(
			"File",
			filters={"file_url": a.file_url, "attached_to_doctype": ["in", ["MS Print Request", "", None]]},
			pluck="name",
		):
			try:
				frappe.delete_doc("File", name, ignore_permissions=True, force=True, delete_permanently=True)
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"print request {request}: file {name}")

	# Permanently — no copy in «Deleted Documents»: an exam paper the principal
	# removed is removed for the teacher, the office and the mobile app alike.
	frappe.delete_doc(
		"MS Print Request", request, ignore_permissions=True, force=True, delete_permanently=True
	)
	frappe.db.commit()
	office = persona in BACK_OFFICE
	return {
		"success": True,
		"data": {"deleted": request},
		"message_en": "Request deleted." if office else "Request withdrawn.",
		"message_ar": "تم حذف الطلب نهائياً لدى الجميع." if office else "تم سحب الطلب.",
	}


def _link_files(doc):
	"""Attach the uploaded files to the request they belong to.

	They are uploaded before the request exists, so they start out attached to
	nothing — and a private file attached to nothing opens for its uploader
	alone, which is how the office got «Forbidden» on every teacher's paper.
	"""
	for a in doc.attachments or []:
		name = frappe.db.get_value("File", {"file_url": a.file_url, "attached_to_name": ["is", "not set"]}, "name")
		if name:
			frappe.db.set_value(
				"File", name,
				{"attached_to_doctype": "MS Print Request", "attached_to_name": doc.name},
				update_modified=False,
			)


def _office_users() -> list[str]:
	"""The secretaries who work the print queue."""
	users = frappe.get_all(
		"Has Role",
		filters={"role": FRAPPE_ROLE_BY_PERSONA[ROLE_SECRETARY], "parenttype": "User"},
		pluck="parent",
	)
	return [
		u for u in users
		if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled")
	]


def _notify_office(doc):
	"""Ring the secretaries' phones for a new request; the bell lists it too."""
	from match_schools import push

	teacher = frappe.db.get_value("User", doc.requested_by, "full_name") or ""
	push.notify(
		_office_users(),
		"طلب طباعة عاجل" if doc.priority == "Urgent" else "طلب طباعة جديد",
		f"{doc.title} — {teacher} · {cint(doc.copies) or 1} نسخة",
		channel="general",
		link="/app/print-requests",
	)


def may_read_request(persona: str, doc) -> bool:
	"""The office, and the teacher who sent it."""
	return persona in BACK_OFFICE or doc.requested_by == frappe.session.user


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def download(request: str = None, file_url: str = None, persona: str = None):
	"""One file of a request, served after the request's own check.

	Frappe's file route decides by the File's owner and the attached record's
	desk permissions, which the portal's personas do not carry; so the office
	was refused files it is meant to print. The check here is the queue's own.
	"""
	doc = frappe.get_doc("MS Print Request", request)
	if not may_read_request(persona, doc):
		frappe.throw(_("This request is not yours."), frappe.PermissionError)
	if not any(a.file_url == file_url for a in doc.attachments or []):
		frappe.throw(_("This file is not part of the request."), frappe.PermissionError)
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		return fail(message_en="File not found.", message_ar="الملف غير موجود.")
	f = frappe.get_doc("File", name)
	frappe.local.response.filename = f.file_name
	frappe.local.response.filecontent = f.get_content()
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "attachment"


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def upload_attachment(persona: str = None):
	"""Store one file for a print request."""
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(message_en="No file received.", message_ar="لم يتم استلام أي ملف.")

	filename = uploaded.filename or "document"
	extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	if extension not in ALLOWED_EXTENSIONS:
		return fail(
			message_en=f"File type .{extension} cannot be printed.",
			message_ar=f"نوع الملف .{extension} غير قابل للطباعة.",
		)

	content = uploaded.stream.read()
	if len(content) > MAX_FILE_BYTES:
		return fail(
			message_en="File is larger than 25 MB.",
			message_ar="حجم الملف يتجاوز ٢٥ ميجابايت.",
		)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			# Private: an exam paper before the exam is the one document in a
			# school that must not be reachable by a guessed URL.
			"is_private": 1,
			"attached_to_doctype": "MS Print Request",
			"attached_to_name": frappe.form_dict.get("request") or None,
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
