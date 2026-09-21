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
from frappe.utils import add_to_date, cint, get_datetime, now

from match_schools.api.utils import (
	apply_period,
	fail,
	ms_endpoint,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
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
	"scheduled": "المجدولة",
	"drafts": "المسودات",
	"archive": "الأرشيف",
	"starred": "المميّزة",
	"trash": "المحذوفات",
}


def _display_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


def _allowed_recipients(persona: str) -> set[str]:
	"""Every user this caller may write to under the school's messaging policy.

	The policy replaced the flat contact list: a school decides per role which
	audiences are reachable, and this is the union of them. Checked on send,
	so an audience the screen no longer offers cannot be reached by a crafted
	request either.
	"""
	from match_schools.api.mail_policy import allowed_users

	return allowed_users(persona)


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
		# A message sent to an audience is described by that audience. Printing
		# two hundred guardian names in a list row is unreadable, and in the
		# To field it is worse — it turns a class circular into a list nobody
		# can scan.
		"audience_key": doc.get("audience_key"),
		"audience_label": doc.get("audience_label"),
		"audience_count": (
			frappe.db.count("MS Message Recipient", {"message": doc.name})
			if doc.get("audience_key")
			else 0
		),
		"attachments": [
			{"file_url": f.file_url, "file_name": f.file_name, "file_size": cint(f.file_size)}
			for f in (doc.files or [])
		],
		"outgoing": doc.sender == viewer,
		# The screen hides the reply button on these; the server refuses the
		# reply regardless, so this is presentation, not protection.
		"no_reply": bool(cint(doc.get("no_reply"))),
		"copy_guardians": bool(cint(doc.get("copy_guardians"))),
		"scheduled_for": str(doc.get("scheduled_for") or ""),
		"is_scheduled": bool(cint(doc.get("is_scheduled"))),
		"send_failed_reason": doc.get("send_failed_reason"),
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
	# A pending row is a scheduled message that has not been sent. The
	# recipient must not be able to open it early by guessing its name.
	return bool(
		frappe.db.exists(
			"MS Message Recipient",
			{"message": doc.name, "user": viewer, "is_pending": 0},
		)
	)


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def folders(persona: str = None):
	"""Folder list with unread counts, for the sidebar."""
	user = frappe.session.user
	# `is_pending` excludes the copies of a scheduled message, which are
	# addressed and stored but have not been delivered yet.
	unread = frappe.db.count(
		"MS Message Recipient",
		{"user": user, "is_read": 0, "is_archived": 0, "is_deleted": 0, "is_pending": 0},
	)
	counts = {
		"inbox": frappe.db.count(
			"MS Message Recipient",
			{"user": user, "is_archived": 0, "is_deleted": 0, "is_pending": 0},
		),
		"starred": frappe.db.count(
			"MS Message Recipient", {"user": user, "is_starred": 1, "is_deleted": 0, "is_pending": 0}
		),
		"archive": frappe.db.count(
			"MS Message Recipient", {"user": user, "is_archived": 1, "is_deleted": 0, "is_pending": 0}
		),
		"trash": frappe.db.count(
			"MS Message Recipient", {"user": user, "is_deleted": 1, "is_pending": 0}
		),
		"sent": frappe.db.count(
			"MS Message",
			{"sender": user, "is_draft": 0, "sender_deleted": 0, "is_scheduled": 0},
		),
		"drafts": frappe.db.count("MS Message", {"sender": user, "is_draft": 1}),
		"scheduled": frappe.db.count("MS Message", {"sender": user, "is_scheduled": 1}),
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

	from match_schools.api.moderation import hidden_authors

	# Mail from someone this reader blocked is delivered — the record stands —
	# but it is not shown to them, in this folder or any other.
	hidden = hidden_authors(user)

	if folder in ("sent", "drafts", "scheduled"):
		filters = {"sender": user, "is_draft": 1 if folder == "drafts" else 0}
		if folder == "sent":
			# A message waiting for its send time is not in the outbox yet; it
			# has its own folder, where it can still be called back.
			filters["sender_deleted"] = 0
			filters["is_scheduled"] = 0
		elif folder == "scheduled":
			filters["is_scheduled"] = 1
		names = frappe.get_all(
			"MS Message",
			filters=filters,
			pluck="name",
			order_by=(
				"scheduled_for asc" if folder == "scheduled" else "sent_on desc, creation desc"
			),
			limit_page_length=limit,
		)
		for n in names:
			doc = frappe.get_doc("MS Message", n)
			rows.append(_message_row(doc, user))
	else:
		mine_filters: dict = {"user": user, "is_pending": 0}
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
			if doc.sender in hidden:
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

	from match_schools.api.moderation import hidden_authors

	hidden = hidden_authors(user)
	if doc.sender in hidden:
		return fail(
			message_en="You have blocked the sender of this message.",
			message_ar="لقد حظرت مُرسِل هذه الرسالة.",
		)

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
			if other.sender in hidden:
				continue
			if _may_read(other, user, persona):
				thread_rows.append(_message_row(other, user, preview_only=False))
	row["thread_messages"] = thread_rows
	return row


def _reply_allowance(data: dict) -> set[str]:
	"""من يجوز الردّ عليهم في هذه الرسالة تحديداً.

	السياسة تحكم **من تبدأ** مراسلته، لا هل تجيب من راسلك. طالبٌ تصله رسالة
	من الإدارة كان يُمنع من الردّ برسالة «يمكنك مراسلة الأشخاص في قائمة جهات
	اتصالك فقط» — لأن الإدارة ليست ضمن جمهوره. وهذا يجعل رسالةً تصله ولا
	سبيل له إلى الجواب، وهو أسوأ من ألّا تصله.

	تُفتح الإذن لطرفَي الرسالة الأصلية وحدهما، ولا تتوسّع: من ردّ على رسالة
	لا يكسب بذلك حقّ مراسلة كل من في المدرسة.
	"""
	parent = (data.get("reply_to") or data.get("in_reply_to") or "").strip()
	if not parent:
		return set()

	me = frappe.session.user
	# لا يُفتح الإذن إلا لمن كان طرفاً في الرسالة الأصلية فعلاً.
	mine = frappe.db.exists(
		"MS Message Recipient", {"message": parent, "user": me, "is_pending": 0}
	)
	sender = frappe.db.get_value("MS Message", parent, "sender")
	if not mine and sender != me:
		return set()

	people = {sender} if sender else set()
	people |= set(
		frappe.get_all(
			"MS Message Recipient",
			filters={"message": parent, "is_pending": 0},
			pluck="user",
		)
	)
	people.discard(me)
	return {p for p in people if p}


def _resolve_recipients(persona: str, data: dict) -> tuple[list[tuple[str, str]], str | None]:
	"""Validate To/Cc/Bcc against the caller's contact list.

	Returns the pairs to store, or an error message. Enforced here rather than
	trusted from the screen: a request naming any other user would otherwise
	reach someone the caller may not contact.
	"""
	pairs: list[tuple[str, str]] = []
	seen: set[str] = set()
	allowed = _allowed_recipients(persona) | _reply_allowance(data)

	# An audience is expanded here, not in the browser. The screen sends
	# "guardians of 4-B", the server decides who that is for this caller, and
	# a stale or forged audience cannot reach anyone the policy excludes.
	audience = (data.get("audience") or "").strip()
	if audience:
		from match_schools.api.mail_policy import AUDIENCES, resolve_audience

		if audience not in AUDIENCES:
			return [], "جمهور غير معروف."
		members = resolve_audience(persona, audience, data.get("audience_groups") or None)
		if not members:
			return [], "لا يوجد مستلمون في هذا الجمهور."
		for user in members:
			if user in seen or user == frappe.session.user:
				continue
			seen.add(user)
			pairs.append((user, "to"))

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

	# A message to a pupil that the family never sees is how a school ends up
	# explaining itself later. When asked, every student recipient's guardians
	# are added as a copy — resolved here, from the student records, rather
	# than typed in by the sender who would have to know each family.
	if cint(data.get("copy_guardians")):
		for user in guardians_of_users([u for u, _kind in pairs]):
			if user not in seen and user != frappe.session.user:
				seen.add(user)
				pairs.append((user, "cc"))

	if len(pairs) > MAX_RECIPIENTS:
		return [], f"الحد الأقصى {MAX_RECIPIENTS} مستلماً للرسالة الواحدة."
	return pairs, None


def guardians_of_users(users: list[str]) -> list[str]:
	"""The guardian accounts behind a list of user accounts.

	Anyone in the list who is not a student is ignored, so this can be handed
	a mixed recipient list without the caller sorting it first.
	"""
	if not users:
		return []

	students = frappe.get_all(
		"Student", filters={"user": ["in", users], "enabled": 1}, pluck="name"
	)
	if not students:
		return []

	guardians = frappe.get_all(
		"Student Guardian",
		filters={"parent": ["in", students], "parenttype": "Student"},
		pluck="guardian",
	)
	if not guardians:
		return []

	accounts = frappe.get_all(
		"Guardian", filters={"name": ["in", list(set(guardians))]}, pluck="user"
	)
	return sorted({u for u in accounts if u})


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

	# A reply to a message that forbids them is refused here rather than only
	# hidden in the screen: the button is gone, but the endpoint is still
	# reachable by anyone who wants to try.
	reply_to = data.get("reply_to")
	if reply_to and cint(frappe.db.get_value("MS Message", reply_to, "no_reply")):
		return fail(
			message_en="This message does not accept replies.",
			message_ar="هذه الرسالة لا تقبل الردود.",
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

	audience = (data.get("audience") or "").strip()
	if audience:
		from match_schools.api.mail_policy import AUDIENCES

		groups = data.get("audience_groups") or []
		labels = [
			frappe.db.get_value("Student Group", g, "student_group_name") or g for g in groups
		]
		doc.audience_key = audience
		doc.audience_label = AUDIENCES[audience]["label_ar"] + (
			f" — {'، '.join(labels)}" if labels else ""
		)
		doc.audience_groups = ",".join(groups)
	else:
		doc.audience_key = None
		doc.audience_label = None
		doc.audience_groups = None

	doc.subject = subject or "(بلا عنوان)"
	doc.body = body
	doc.is_draft = 1 if as_draft else 0
	doc.no_reply = 1 if cint(data.get("no_reply")) else 0
	doc.copy_guardians = 1 if cint(data.get("copy_guardians")) else 0

	# A send date in the past is a send now, not an error: the sender meant
	# "go", and refusing over a minute's clock drift helps nobody.
	scheduled = (data.get("scheduled_for") or "").strip() or None
	if scheduled and get_datetime(scheduled) <= get_datetime(now()):
		scheduled = None
	if scheduled and get_datetime(scheduled) > add_to_date(get_datetime(now()), years=1):
		return fail(
			message_en="A message cannot be scheduled more than a year ahead.",
			message_ar="لا يمكن جدولة رسالة لأكثر من سنة مقدماً.",
		)
	doc.scheduled_for = scheduled if not as_draft else None
	doc.is_scheduled = 1 if doc.scheduled_for else 0
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

	# A scheduled message has not been sent yet, so it carries no sent time —
	# that is what keeps it out of the recipients' folders until it is due.
	if not as_draft and not doc.scheduled_for:
		doc.sent_on = now()

	doc.save(ignore_permissions=True)
	if not doc.thread and not doc.reply_to:
		doc.db_set("thread", doc.name, update_modified=False)

	# The child rows carry the delivery state, so they are (re)created for a
	# real send. A draft has none: nothing has been delivered.
	frappe.db.delete("MS Message Recipient", {"message": doc.name})
	if not as_draft and not doc.scheduled_for:
		_deliver(doc, pairs)

	# A scheduled message keeps its resolved recipients so the audience is
	# fixed at the moment of writing. Re-resolving at send time would let a
	# class list that changed in between quietly redirect the message.
	if doc.scheduled_for:
		_hold(doc, pairs)

	frappe.db.commit()

	if as_draft:
		return {
			"success": True,
			"data": {"id": doc.name, "is_draft": True, "recipients": len(pairs)},
			"message_en": "Draft saved.",
			"message_ar": "تم حفظ المسودة.",
		}
	if doc.scheduled_for:
		when = str(doc.scheduled_for)[:16]
		return {
			"success": True,
			"data": {
				"id": doc.name,
				"is_draft": False,
				"scheduled_for": str(doc.scheduled_for),
				"recipients": len(pairs),
			},
			"message_en": f"Scheduled for {when} to {len(pairs)} recipient(s).",
			"message_ar": f"تمت جدولتها في {when} إلى {len(pairs)} مستلماً.",
		}
	return {
		"success": True,
		"data": {"id": doc.name, "is_draft": False, "recipients": len(pairs)},
		"message_en": f"Sent to {len(pairs)} recipient(s).",
		"message_ar": f"أُرسلت إلى {len(pairs)} مستلماً.",
	}


def _deliver(doc, pairs: list[tuple[str, str]]) -> None:
	"""Put one copy in each recipient's mailbox, then ring their phones.

	Someone who blocked the sender is dropped here rather than filtered on
	read: no row, no unread count, no phone ringing at midnight. The sender is
	told nothing — a block that announces itself invites the next message.
	"""
	from match_schools.api.moderation import blocked_between

	undeliverable = blocked_between(doc.sender, [u for u, _k in pairs])
	pairs = [(u, k) for u, k in pairs if u not in undeliverable]
	for user, kind in pairs:
		frappe.get_doc(
			{
				"doctype": "MS Message Recipient",
				"message": doc.name,
				"user": user,
				"kind": kind,
				"is_read": 0,
				"is_pending": 0,
			}
		).insert(ignore_permissions=True)

	_push(doc, [u for u, _k in pairs])


def _push(doc, users: list[str]) -> None:
	"""Send the phone notification for a delivered message.

	The sender never gets their own copy — they are on the thread but they
	just wrote it. Failure here is swallowed inside `push.notify`: a message
	that reached the mailbox has been delivered whether or not the phone rang.
	"""
	from match_schools import push

	audience = [u for u in users if u and u != doc.sender]
	if not audience:
		return

	sender = frappe.db.get_value("User", doc.sender, "full_name") or doc.sender
	subject = (doc.subject or "").strip() or "رسالة جديدة"
	push.notify(
		audience,
		sender,
		subject[:120],
		channel="messages",
		type="message",
		id=doc.name,
		thread=doc.thread or doc.name,
	)


def _hold(doc, pairs: list[tuple[str, str]]) -> None:
	"""Store the resolved recipients of a scheduled message, undelivered.

	The rows are marked pending so every mailbox query skips them: the message
	exists, addressed and fixed, but has not arrived.
	"""
	for user, kind in pairs:
		frappe.get_doc(
			{
				"doctype": "MS Message Recipient",
				"message": doc.name,
				"user": user,
				"kind": kind,
				"is_read": 0,
				"is_pending": 1,
			}
		).insert(ignore_permissions=True)


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
		"Student Group",
		filters=apply_period({"disabled": 0}, "Student Group"),
		pluck="name",
		limit_page_length=0,
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


# ---------------------------------------------------------------------------
# Scheduled sending
# ---------------------------------------------------------------------------


def deliver_due_messages():
	"""Release scheduled messages whose time has come.

	Runs on the scheduler. Each message is committed on its own: one bad row
	must not hold back the rest of the batch, and a message that has already
	been released must never be released twice — the recipient rows are
	flipped in one statement and the flag cleared with them.
	"""
	due = frappe.get_all(
		"MS Message",
		filters={
			"is_scheduled": 1,
			"is_draft": 0,
			"scheduled_for": ["<=", now()],
		},
		pluck="name",
		limit_page_length=200,
	)

	for name in due:
		try:
			doc = frappe.get_doc("MS Message", name)
			frappe.db.set_value(
				"MS Message",
				name,
				{"is_scheduled": 0, "sent_on": doc.scheduled_for or now(), "send_failed_reason": None},
				update_modified=False,
			)
			held = frappe.get_all(
				"MS Message Recipient",
				filters={"message": name, "is_pending": 1},
				pluck="user",
			)
			frappe.db.sql(
				"""update `tabMS Message Recipient`
				      set is_pending = 0
				    where message = %s and is_pending = 1""",
				name,
			)
			frappe.db.commit()
			# The phone alert belongs to the moment the message actually
			# arrives, not to the moment it was written and parked.
			_push(doc, held)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="تعذّر إرسال رسالة مجدولة", message=f"{name}\n{frappe.get_traceback()}"
			)
			frappe.db.set_value(
				"MS Message",
				name,
				"send_failed_reason", "تعذّر الإرسال — يرجى مراجعة الرسالة.",
				update_modified=False,
			)
			frappe.db.commit()


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def cancel_schedule(message: str = None, persona: str = None):
	"""Pull a scheduled message back before it goes out.

	It returns to drafts rather than vanishing: the sender wrote it, and the
	usual reason for cancelling is to change something and send it again.
	"""
	if not message:
		return fail(message_en="A message is required.", message_ar="يجب تحديد الرسالة.")

	doc = frappe.get_doc("MS Message", message)
	if doc.sender != frappe.session.user:
		frappe.throw(_("This message is not yours."), frappe.PermissionError)
	if not cint(doc.is_scheduled):
		return fail(
			message_en="This message is not scheduled.",
			message_ar="هذه الرسالة غير مجدولة.",
		)

	frappe.db.delete("MS Message Recipient", {"message": doc.name})
	doc.db_set(
		{"is_scheduled": 0, "scheduled_for": None, "is_draft": 1, "sent_on": None},
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Moved back to drafts.",
		"message_ar": "أُعيدت إلى المسودات.",
	}


# ---------------------------------------------------------------------------
# Who will actually receive this
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def preview_recipients(payload: str | dict = None, persona: str = None):
	"""The exact list this message would go to, before it is sent.

	An audience is a promise the sender cannot check by eye — "guardians of my
	classes" could be nine people or ninety, and the guardian copy adds names
	nobody typed. Resolved through the same function that the send uses, so
	what is shown here is what would actually be delivered, not a second
	implementation that can drift from it.
	"""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})

	pairs, error = _resolve_recipients(persona, data)
	if error:
		return fail(message_en="Recipient not allowed.", message_ar=error)

	users = [u for u, _k in pairs]
	kinds = dict((u, k) for u, k in pairs)
	if not users:
		return {"recipients": [], "total": 0, "groups": []}

	rows = frappe.get_all(
		"User",
		filters={"name": ["in", users]},
		fields=["name", "full_name", "enabled"],
		limit_page_length=0,
	)
	by_name = {r.name: r for r in rows}

	# Which kind of person each account belongs to, so the sender can see
	# "28 guardians, 3 teachers" rather than a wall of email addresses.
	students = {
		r.user: r.student_name
		for r in frappe.get_all(
			"Student", filters={"user": ["in", users]}, fields=["user", "student_name"]
		)
		if r.user
	}
	guardians = {
		r.user: r.guardian_name
		for r in frappe.get_all(
			"Guardian", filters={"user": ["in", users]}, fields=["user", "guardian_name"]
		)
		if r.user
	}

	out = []
	tally = {"student": 0, "guardian": 0, "staff": 0, "disabled": 0}
	for user in users:
		info = by_name.get(user)
		if user in students:
			kind, label = "student", students[user]
		elif user in guardians:
			kind, label = "guardian", guardians[user]
		else:
			kind, label = "staff", (info.full_name if info else user)
		tally[kind] += 1
		enabled = bool(info and info.enabled)
		if not enabled:
			tally["disabled"] += 1
		out.append(
			{
				"user": user,
				"name": label or user,
				"kind": kind,
				"copy": kinds.get(user, "to"),
				"enabled": enabled,
			}
		)

	out.sort(key=lambda r: (r["kind"], r["name"] or ""))
	groups = [
		{"kind": "student", "label": "طلاب", "count": tally["student"]},
		{"kind": "guardian", "label": "أولياء أمور", "count": tally["guardian"]},
		{"kind": "staff", "label": "موظفون", "count": tally["staff"]},
	]
	return {
		"recipients": out,
		"total": len(out),
		"groups": [g for g in groups if g["count"]],
		"disabled": tally["disabled"],
	}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

# Filled in when a template is applied. Deliberately short and obvious: a
# teacher types these by hand, so anything longer would not be used.
PLACEHOLDERS = {
	"{اسم_الطالب}": "student_name",
	"{اسم_الصف}": "group_name",
	"{اسم_المادة}": "course_name",
	"{اسم_المرسل}": "sender_name",
	"{التاريخ}": "today",
}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def list_templates(category: str = None, persona: str = None):
	"""The caller's own templates, plus the ones colleagues have shared."""
	user = frappe.session.user
	filters = [["MS Mail Template", "owner_user", "=", user]]

	mine = frappe.get_all(
		"MS Mail Template",
		filters={"owner_user": user},
		fields=["name", "title", "category", "subject", "body", "is_shared", "use_count"],
		order_by="use_count desc, modified desc",
		limit_page_length=0,
	)
	shared = frappe.get_all(
		"MS Mail Template",
		filters={"is_shared": 1, "owner_user": ["!=", user]},
		fields=[
			"name", "title", "category", "subject", "body", "is_shared",
			"use_count", "owner_user",
		],
		order_by="use_count desc, modified desc",
		limit_page_length=0,
	)

	names = {r.owner_user for r in shared}
	full = (
		{
			r.name: r.full_name
			for r in frappe.get_all(
				"User", filters={"name": ["in", list(names)]}, fields=["name", "full_name"]
			)
		}
		if names
		else {}
	)

	out = []
	for r in mine:
		out.append({**r, "mine": True, "owner_name": None})
	for r in shared:
		out.append({**r, "mine": False, "owner_name": full.get(r.owner_user) or r.owner_user})

	if category:
		out = [r for r in out if r.get("category") == category]

	return {
		"templates": out,
		"placeholders": [{"token": k, "field": v} for k, v in PLACEHOLDERS.items()],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def save_template(payload: str | dict = None, persona: str = None):
	"""Create or update one of the caller's own templates."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	title = (data.get("title") or "").strip()
	if not title:
		return fail(message_en="A name is required.", message_ar="اسم القالب مطلوب.")

	body = data.get("body") or ""
	if len(body) > MAX_BODY:
		return fail(message_en="The template is too long.", message_ar="نص القالب طويل جداً.")

	name = data.get("template")
	if name:
		doc = frappe.get_doc("MS Mail Template", name)
		# Shared means readable, never editable: a colleague's template is
		# theirs, and a copy is what the reader actually wants.
		if doc.owner_user != frappe.session.user:
			frappe.throw(_("This template is not yours."), frappe.PermissionError)
	else:
		if frappe.db.count("MS Mail Template", {"owner_user": frappe.session.user}) >= 100:
			return fail(
				message_en="You already have 100 templates.",
				message_ar="لديك 100 قالب بالفعل — احذف واحداً قبل إضافة غيره.",
			)
		doc = frappe.new_doc("MS Mail Template")
		doc.owner_user = frappe.session.user

	doc.title = title[:140]
	doc.category = data.get("category") or "عام"
	doc.subject = (data.get("subject") or "").strip()[:MAX_SUBJECT]
	doc.body = body
	doc.is_shared = 1 if cint(data.get("is_shared")) else 0
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Template saved.",
		"message_ar": "تم حفظ القالب.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def delete_template(template: str = None, persona: str = None):
	"""Remove one of the caller's own templates."""
	if not template:
		return fail(message_en="A template is required.", message_ar="يجب تحديد القالب.")

	doc = frappe.get_doc("MS Mail Template", template)
	if doc.owner_user != frappe.session.user:
		frappe.throw(_("This template is not yours."), frappe.PermissionError)

	frappe.delete_doc("MS Mail Template", template, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {},
		"message_en": "Template deleted.",
		"message_ar": "تم حذف القالب.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def apply_template(
	template: str = None, student: str = None, student_group: str = None,
	course: str = None, persona: str = None,
):
	"""A template with its placeholders filled in for this context.

	Substitution happens on the server so the tokens have one meaning. Doing
	it in the browser would leave `{اسم_الطالب}` in the sent copy whenever a
	screen forgot to run the same replacement.
	"""
	if not template:
		return fail(message_en="A template is required.", message_ar="يجب تحديد القالب.")

	doc = frappe.get_doc("MS Mail Template", template)
	if doc.owner_user != frappe.session.user and not cint(doc.is_shared):
		frappe.throw(_("This template is not shared with you."), frappe.PermissionError)

	values = {
		"{اسم_الطالب}": (
			frappe.db.get_value("Student", student, "student_name") if student else ""
		) or "",
		"{اسم_الصف}": (
			frappe.db.get_value("Student Group", student_group, "student_group_name")
			if student_group
			else ""
		) or "",
		"{اسم_المادة}": (
			frappe.db.get_value("Course", course, "course_name") if course else ""
		) or "",
		"{اسم_المرسل}": _display_name(frappe.session.user),
		"{التاريخ}": frappe.utils.formatdate(frappe.utils.today(), "dd/MM/yyyy"),
	}

	subject, body = doc.subject or "", doc.body or ""
	for token, value in values.items():
		subject = subject.replace(token, value)
		body = body.replace(token, value)

	frappe.db.set_value(
		"MS Mail Template", doc.name, "use_count", cint(doc.use_count) + 1,
		update_modified=False,
	)
	frappe.db.commit()

	return {"subject": subject, "body": body, "title": doc.title}
