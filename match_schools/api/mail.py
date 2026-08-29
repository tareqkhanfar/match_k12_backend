"""Messaging as a mailbox rather than a chat.

A chat window is the wrong shape for a school. A teacher writing to thirty
guardians about a trip is not having thirty conversations; a head sending a
circular to the staff needs it to land in an inbox, not scroll away in a
thread. So: folders, To/Cc/Bcc, attachments, replies, and a read state that
belongs to each recipient rather than to the message.

The mailbox is per person. One message, many `MS Message Recipient` rows —
unread for one, archived by another, starred by a third. Storing that on the
message would make "read" mean whichever recipient opened it last.

Bcc is a promise: a blind copy must not appear in anyone else's recipient
list, including in a reply-all. `_visible_recipients` is the single place that
decides who a reader may see, and every path returns through it.

Who may be written to is `communication.contacts` — the same list that already
governs direct messages, so a student cannot mail another student here either.
"""

import frappe
from frappe import _
from frappe.utils import cint, now

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
ALL_ROLES = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)

MAX_RECIPIENTS = 200
MAX_SUBJECT = 200
MAX_BODY = 50_000
MAX_FILES = 10
MAX_FILE_BYTES = 15 * 1024 * 1024

ALLOWED_EXTENSIONS = {
	"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt",
	"png", "jpg", "jpeg", "gif", "webp",
	"zip",
}

FOLDER_AR = {
	"inbox": "الوارد",
	"sent": "الصادر",
	"drafts": "المسودات",
	"archive": "الأرشيف",
	"starred": "المميّزة",
	"trash": "المحذوفات",
}


def _display_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


def _allowed_recipients(persona: str) -> set[str]:
	"""The users this caller may write to, from the shared contact list."""
	from match_schools.api.communication import contacts

	result = contacts(persona=persona)
	people = result.get("data") if isinstance(result, dict) else result
	return {c["user"] for c in (people or []) if isinstance(c, dict) and c.get("user")}


def _visible_recipients(doc, viewer: str) -> list[dict]:
	"""Who a given reader is allowed to see on a message.

	Bcc is only ever visible to the sender and to the blind recipient
	themselves. Leaking it in a thread view or a reply-all would break the one
	guarantee the field makes.
	"""
	is_sender = doc.sender == viewer
	rows = frappe.get_all(
		"MS Message Recipient",
		filters={"message": doc.name},
		fields=["user", "kind"],
		limit_page_length=0,
	)
	out = []
	for r in rows:
		if r.kind == "bcc" and not is_sender and r.user != viewer:
			continue
		out.append(
			{
				"user": r.user,
				"name": _display_name(r.user),
				"kind": r.kind,
			}
		)
	return out


def _message_row(doc, viewer: str, mine=None, preview_only: bool = True) -> dict:
	"""One message as this reader sees it."""
	body = doc.body or ""
	row = {
		"id": doc.name,
		"subject": doc.subject,
		"thread": doc.thread,
		"sender": doc.sender,
		"sender_name": _display_name(doc.sender),
		"sent_on": str(doc.sent_on or ""),
		"is_draft": bool(cint(doc.is_draft)),
		"reply_to": doc.reply_to,
		"about_student": doc.about_student,
		"recipients": _visible_recipients(doc, viewer),
		"attachments": [
			{"file_url": f.file_url, "file_name": f.file_name, "file_size": cint(f.file_size)}
			for f in (doc.files or [])
		],
		"outgoing": doc.sender == viewer,
	}
	if preview_only:
		# The list shows a plain-text snippet: rendering HTML in a row makes
		# the list jump around and a long message push everything off screen.
		text = frappe.utils.strip_html(body or "").strip()
		row["preview"] = text[:140]
	else:
		row["body"] = body

	if mine is not None:
		row.update(
			{
				"is_read": bool(cint(mine.is_read)),
				"is_starred": bool(cint(mine.is_starred)),
				"is_archived": bool(cint(mine.is_archived)),
				"my_kind": mine.kind,
			}
		)
	else:
		row.update(
			{
				"is_read": True,
				"is_starred": False,
				"is_archived": bool(cint(doc.sender_archived)),
				"my_kind": None,
			}
		)
	return row


def _may_read(doc, viewer: str, persona: str) -> bool:
	if persona in BACK_OFFICE and doc.sender == viewer:
		return True
	if doc.sender == viewer:
		return True
	return bool(
		frappe.db.exists("MS Message Recipient", {"message": doc.name, "user": viewer})
	)


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def folders(persona: str = None):
	"""Folder list with unread counts, for the sidebar."""
	user = frappe.session.user
	unread = frappe.db.count(
		"MS Message Recipient",
		{"user": user, "is_read": 0, "is_archived": 0, "is_deleted": 0},
	)
	counts = {
		"inbox": frappe.db.count(
			"MS Message Recipient",
			{"user": user, "is_archived": 0, "is_deleted": 0},
		),
		"starred": frappe.db.count(
			"MS Message Recipient", {"user": user, "is_starred": 1, "is_deleted": 0}
		),
		"archive": frappe.db.count(
			"MS Message Recipient", {"user": user, "is_archived": 1, "is_deleted": 0}
		),
		"trash": frappe.db.count("MS Message Recipient", {"user": user, "is_deleted": 1}),
		"sent": frappe.db.count(
			"MS Message", {"sender": user, "is_draft": 0, "sender_deleted": 0}
		),
		"drafts": frappe.db.count("MS Message", {"sender": user, "is_draft": 1}),
	}
	return {
		"unread": unread,
		"counts": counts,
		"folders": [{"key": k, "label": v, "count": counts.get(k, 0)} for k, v in FOLDER_AR.items()],
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def list_messages(
	folder: str = "inbox",
	search: str = None,
	unread_only: int = 0,
	limit: int = 50,
	persona: str = None,
):
	"""One folder of the caller's mailbox."""
	user = frappe.session.user
	folder = (folder or "inbox").lower()
	if folder not in FOLDER_AR:
		return fail(message_en="Unknown folder.", message_ar="مجلد غير معروف.")

	limit = min(max(cint(limit) or 50, 1), 200)
	rows: list[dict] = []

	if folder in ("sent", "drafts"):
		filters = {"sender": user, "is_draft": 1 if folder == "drafts" else 0}
		if folder == "sent":
			filters["sender_deleted"] = 0
		names = frappe.get_all(
			"MS Message",
			filters=filters,
			pluck="name",
			order_by="sent_on desc, creation desc",
			limit_page_length=limit,
		)
		for n in names:
			doc = frappe.get_doc("MS Message", n)
			rows.append(_message_row(doc, user))
	else:
		mine_filters: dict = {"user": user}
		if folder == "inbox":
			mine_filters.update({"is_archived": 0, "is_deleted": 0})
		elif folder == "archive":
			mine_filters.update({"is_archived": 1, "is_deleted": 0})
		elif folder == "starred":
			mine_filters.update({"is_starred": 1, "is_deleted": 0})
		elif folder == "trash":
			mine_filters["is_deleted"] = 1
		if cint(unread_only):
			mine_filters["is_read"] = 0

		mine_rows = frappe.get_all(
			"MS Message Recipient",
			filters=mine_filters,
			fields=["name", "message", "kind", "is_read", "is_starred", "is_archived"],
			order_by="creation desc",
			limit_page_length=limit,
		)
		for m in mine_rows:
			if not frappe.db.exists("MS Message", m.message):
				continue
			doc = frappe.get_doc("MS Message", m.message)
			# A draft is not delivered; a recipient row for one would be a bug,
			# but filtering here means it can never surface as a mystery mail.
			if cint(doc.is_draft):
				continue
			rows.append(_message_row(doc, user, m))

	if search:
		needle = search.strip().lower()
		rows = [
			r
			for r in rows
			if needle in (r.get("subject") or "").lower()
			or needle in (r.get("preview") or "").lower()
			or needle in (r.get("sender_name") or "").lower()
		]

	return {
		"messages": rows,
		"folder": folder,
		"folder_label": FOLDER_AR[folder],
		"total": len(rows),
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def get_message(message: str = None, persona: str = None):
	"""One message in full, and the thread it belongs to."""
	if not message:
		return fail(message_en="A message is required.", message_ar="يجب تحديد الرسالة.")
	doc = frappe.get_doc("MS Message", message)
	user = frappe.session.user
	if not _may_read(doc, user, persona):
		frappe.throw(_("You are not part of this conversation."), frappe.PermissionError)

	mine = None
	mine_name = frappe.db.get_value(
		"MS Message Recipient", {"message": message, "user": user}, "name"
	)
	if mine_name:
		mine = frappe.get_doc("MS Message Recipient", mine_name)
		if not cint(mine.is_read):
			# Opening it is what marks it read; a separate "mark read" call
			# would leave the count wrong whenever the user simply reads.
			mine.is_read = 1
			mine.read_on = now()
			mine.save(ignore_permissions=True)
			frappe.db.commit()

	row = _message_row(doc, user, mine, preview_only=False)

	# The rest of the thread this caller may see.
	thread_rows = []
	if doc.thread:
		for n in frappe.get_all(
			"MS Message",
			filters={"thread": doc.thread, "is_draft": 0, "name": ["!=", message]},
			pluck="name",
			order_by="sent_on asc",
			limit_page_length=50,
		):
			other = frappe.get_doc("MS Message", n)
			if _may_read(other, user, persona):
				thread_rows.append(_message_row(other, user, preview_only=False))
	row["thread_messages"] = thread_rows
	return row


def _resolve_recipients(persona: str, data: dict) -> tuple[list[tuple[str, str]], str | None]:
	"""Validate To/Cc/Bcc against the caller's contact list.

	Returns the pairs to store, or an error message. Enforced here rather than
	trusted from the screen: a request naming any other user would otherwise
	reach someone the caller may not contact.
	"""
	pairs: list[tuple[str, str]] = []
	seen: set[str] = set()
	allowed = _allowed_recipients(persona)

	for kind in ("to", "cc", "bcc"):
		for user in data.get(kind) or []:
			if not user or user in seen or user == frappe.session.user:
				continue
			if not frappe.db.exists("User", user):
				return [], f"لم يتم العثور على المستخدم {user}."
			if persona not in BACK_OFFICE and user not in allowed:
				return [], "يمكنك مراسلة الأشخاص في قائمة جهات اتصالك فقط."
			seen.add(user)
			pairs.append((user, kind))

	if len(pairs) > MAX_RECIPIENTS:
		return [], f"الحد الأقصى {MAX_RECIPIENTS} مستلماً للرسالة الواحدة."
	return pairs, None


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def save_message(payload: str | dict = None, persona: str = None):
	"""Send a message, or keep it as a draft."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	message = data.get("message")
	as_draft = cint(data.get("is_draft"))

	subject = (data.get("subject") or "").strip()
	body = data.get("body") or ""
	if len(subject) > MAX_SUBJECT:
		return fail(
			message_en=f"The subject is at most {MAX_SUBJECT} characters.",
			message_ar=f"الحد الأقصى لعنوان الرسالة {MAX_SUBJECT} حرف.",
		)
	if len(body) > MAX_BODY:
		return fail(
			message_en="The message is too long.",
			message_ar="نص الرسالة طويل جداً.",
		)

	if message:
		doc = frappe.get_doc("MS Message", message)
		if doc.sender != frappe.session.user:
			frappe.throw(_("This message is not yours."), frappe.PermissionError)
		if not cint(doc.is_draft):
			# A sent message is a record. Editing it would change what the
			# recipient already read.
			frappe.throw(_("A sent message cannot be changed."), frappe.PermissionError)
	else:
		doc = frappe.new_doc("MS Message")
		doc.sender = frappe.session.user

	pairs, error = _resolve_recipients(persona, data)
	if error:
		return fail(message_en="Recipient not allowed.", message_ar=error)
	if not as_draft and not pairs:
		return fail(
			message_en="Add at least one recipient.",
			message_ar="أضف مستلماً واحداً على الأقل.",
		)
	if not as_draft and not subject:
		return fail(message_en="A subject is required.", message_ar="عنوان الرسالة مطلوب.")

	doc.subject = subject or "(بلا عنوان)"
	doc.body = body
	doc.is_draft = 1 if as_draft else 0
	doc.about_student = data.get("about_student")
	doc.reply_to = data.get("reply_to")

	# A reply stays in its thread; a new message starts one named after itself
	# so the whole conversation can be found from any message in it.
	if doc.reply_to:
		doc.thread = frappe.db.get_value("MS Message", doc.reply_to, "thread") or doc.reply_to
	elif not doc.thread:
		doc.thread = None

	files = data.get("attachments")
	if files is not None:
		if len(files) > MAX_FILES:
			return fail(
				message_en=f"At most {MAX_FILES} files per message.",
				message_ar=f"الحد الأقصى {MAX_FILES} ملفات للرسالة الواحدة.",
			)
		doc.set("files", [])
		for f in files:
			url = (f or {}).get("file_url")
			if url:
				doc.append(
					"files",
					{
						"file_url": url,
						"file_name": f.get("file_name"),
						"file_size": cint(f.get("file_size")),
						"uploaded_on": now(),
					},
				)

	# The original single-recipient field is still on the doctype and still
	# read by the older conversation view, so it carries the first To. Leaving
	# it empty would both fail validation and make old threads lose their
	# other side.
	doc.recipient = next((u for u, k in pairs if k == "to"), None) or (
		pairs[0][0] if pairs else None
	)

	if not as_draft:
		doc.sent_on = now()
	if not doc.ms_academic_year:
		doc.ms_academic_year = get_default_academic_year()
	if not doc.ms_academic_term:
		doc.ms_academic_term = get_default_academic_term()

	doc.save(ignore_permissions=True)
	if not doc.thread and not doc.reply_to:
		doc.db_set("thread", doc.name, update_modified=False)

	# The child rows carry the delivery state, so they are (re)created for a
	# real send. A draft has none: nothing has been delivered.
	frappe.db.delete("MS Message Recipient", {"message": doc.name})
	if not as_draft:
		for user, kind in pairs:
			frappe.get_doc(
				{
					"doctype": "MS Message Recipient",
					"message": doc.name,
					"user": user,
					"kind": kind,
					"is_read": 0,
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "is_draft": bool(as_draft), "recipients": len(pairs)},
		"message_en": "Draft saved." if as_draft else f"Sent to {len(pairs)} recipient(s).",
		"message_ar": "تم حفظ المسودة." if as_draft else f"أُرسلت إلى {len(pairs)} مستلماً.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def set_flags(
	message: str = None,
	is_read: int = None,
	is_starred: int = None,
	is_archived: int = None,
	is_deleted: int = None,
	persona: str = None,
):
	"""Move a message between folders, for this reader only."""
	if not message:
		return fail(message_en="A message is required.", message_ar="يجب تحديد الرسالة.")
	user = frappe.session.user

	name = frappe.db.get_value(
		"MS Message Recipient", {"message": message, "user": user}, "name"
	)
	if name:
		row = frappe.get_doc("MS Message Recipient", name)
		if is_read is not None:
			row.is_read = cint(is_read)
			row.read_on = now() if cint(is_read) else None
		if is_starred is not None:
			row.is_starred = cint(is_starred)
		if is_archived is not None:
			row.is_archived = cint(is_archived)
		if is_deleted is not None:
			row.is_deleted = cint(is_deleted)
		row.save(ignore_permissions=True)
	else:
		# The sender's own copy: they have no recipient row, so their folder
		# state lives on the message.
		doc = frappe.get_doc("MS Message", message)
		if doc.sender != user:
			frappe.throw(_("You are not part of this conversation."), frappe.PermissionError)
		if is_archived is not None:
			doc.sender_archived = cint(is_archived)
		if is_deleted is not None:
			doc.sender_deleted = cint(is_deleted)
		doc.save(ignore_permissions=True)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": message},
		"message_en": "Updated.",
		"message_ar": "تم التحديث.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def mark_all_read(persona: str = None):
	"""Clear the unread count in one action."""
	user = frappe.session.user
	names = frappe.get_all(
		"MS Message Recipient",
		filters={"user": user, "is_read": 0, "is_deleted": 0},
		pluck="name",
	)
	for n in names:
		frappe.db.set_value("MS Message Recipient", n, {"is_read": 1, "read_on": now()},
			update_modified=False)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"marked": len(names)},
		"message_en": f"Marked {len(names)} as read.",
		"message_ar": f"تم تعليم {len(names)} رسالة كمقروءة.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def delete_draft(message: str = None, persona: str = None):
	"""Discard a draft. Only a draft — a sent message is a record."""
	if not message:
		return fail(message_en="A message is required.", message_ar="يجب تحديد الرسالة.")
	doc = frappe.get_doc("MS Message", message)
	if doc.sender != frappe.session.user:
		frappe.throw(_("This message is not yours."), frappe.PermissionError)
	if not cint(doc.is_draft):
		frappe.throw(_("A sent message cannot be deleted."), frappe.PermissionError)

	for f in doc.files or []:
		name = frappe.db.get_value("File", {"file_url": f.file_url}, "name")
		if name:
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	frappe.db.delete("MS Message Recipient", {"message": message})
	frappe.delete_doc("MS Message", message, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"deleted": message},
		"message_en": "Draft discarded.",
		"message_ar": "تم حذف المسودة.",
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def search_recipients(q: str = None, limit: int = 25, persona: str = None):
	"""Find someone to write to, by name, id number or system id.

	A school knows a child by their national id as often as by name, and staff
	know the system id from every printed list. Searching one field only would
	send people back to a paper roster.
	"""
	allowed = _allowed_recipients(persona)
	if not allowed:
		return {"results": []}

	needle = (q or "").strip()
	limit = min(max(cint(limit) or 25, 1), 100)

	users = frappe.get_all(
		"User",
		filters={"name": ["in", list(allowed)], "enabled": 1},
		fields=["name", "full_name", "user_image"],
		limit_page_length=0,
	)
	by_user = {u.name: u for u in users}

	# The school's own identifiers, so a search can match them.
	extra: dict[str, dict] = {}
	for row in frappe.get_all(
		"Student",
		filters={"user": ["in", list(allowed)]},
		fields=["name", "user", "student_name", "ms_id_number"],
		limit_page_length=0,
	):
		extra[row.user] = {
			"record": row.name,
			"national_id": row.get("ms_id_number"),
			"kind": "student",
		}
	for row in frappe.get_all(
		"Guardian",
		filters={"user": ["in", list(allowed)]},
		fields=["name", "user", "guardian_name"],
		limit_page_length=0,
	):
		extra.setdefault(row.user, {"record": row.name, "national_id": None, "kind": "parent"})

	results = []
	for user_id, u in by_user.items():
		meta = extra.get(user_id, {})
		haystack = " ".join(
			str(x or "")
			for x in (u.full_name, user_id, meta.get("record"), meta.get("national_id"))
		).lower()
		if needle and needle.lower() not in haystack:
			continue
		results.append(
			{
				"user": user_id,
				"name": u.full_name or user_id,
				"email": user_id,
				"record": meta.get("record"),
				"national_id": meta.get("national_id"),
				"kind": meta.get("kind"),
				"image": u.user_image,
			}
		)
		if len(results) >= limit:
			break

	results.sort(key=lambda r: r["name"] or "")
	return {"results": results}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def recipient_groups(persona: str = None):
	"""Ready-made groups, so a circular is not addressed one name at a time.

	Only what the caller may already write to: a group is a shortcut through
	the contact list, never a way around it.
	"""
	allowed = _allowed_recipients(persona)
	if not allowed:
		return {"groups": []}

	groups = []

	# The guardians of each class the caller is connected to.
	from match_schools.api.community import _my_groups

	for g in _my_groups(persona) if persona == ROLE_TEACHER else frappe.get_all(
		"Student Group", filters={"disabled": 0}, pluck="name", limit_page_length=0
	):
		students = frappe.get_all(
			"Student Group Student",
			filters={"parent": g, "active": 1},
			pluck="student",
		)
		if not students:
			continue
		label = frappe.db.get_value("Student Group", g, "student_group_name") or g

		guardians = {
			frappe.db.get_value("Guardian", r.guardian, "user")
			for r in frappe.get_all(
				"Student Guardian",
				filters={"parent": ["in", students], "parenttype": "Student"},
				fields=["guardian"],
				limit_page_length=0,
			)
		}
		guardians = sorted(u for u in guardians if u and u in allowed)
		if guardians:
			groups.append(
				{
					"key": f"guardians::{g}",
					"label": f"أولياء أمور {label}",
					"count": len(guardians),
					"users": guardians,
				}
			)

		pupils = sorted(
			u
			for u in frappe.get_all(
				"Student", filters={"name": ["in", students]}, pluck="user"
			)
			if u and u in allowed
		)
		if pupils:
			groups.append(
				{
					"key": f"students::{g}",
					"label": f"طلاب {label}",
					"count": len(pupils),
					"users": pupils,
				}
			)

	if persona in BACK_OFFICE:
		teachers = sorted(
			u
			for u in frappe.get_all(
				"User",
				filters={"name": ["in", list(allowed)], "enabled": 1},
				pluck="name",
			)
			if frappe.db.exists("Has Role", {"parent": u, "role": "MS Teacher"})
		)
		if teachers:
			groups.append(
				{
					"key": "staff::teachers",
					"label": "جميع المعلمين",
					"count": len(teachers),
					"users": teachers,
				}
			)

	return {"groups": groups}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def upload_attachment(persona: str = None):
	"""Store one file for a message."""
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
	if len(content) > MAX_FILE_BYTES:
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
			"attached_to_doctype": "MS Message",
			"attached_to_name": frappe.form_dict.get("message") or None,
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
