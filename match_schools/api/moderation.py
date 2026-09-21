"""Reporting abusive content, and blocking the person who wrote it.

Two obligations meet here. The school's own duty of care over what children
read, and the app stores' rule that any app carrying content written by its
users must let a reader report that content and block its author (Apple's
guideline 1.2; Google's UGC policy). Staff moderation alone does not satisfy
either store: the mechanism has to be in the reader's hands.

The design keeps the two apart on purpose:

- A **report** is a request to the school. It creates a record for the office
  to act on and changes nothing for anyone else. Abuse of the feature costs
  the reporter nothing and the school a glance.
- A **block** is the reader's own decision and takes effect immediately, with
  no one to wait for. It hides the blocked person's comments and mail from the
  blocker, in both directions, so a blocked person cannot keep reaching the
  reader by writing again.

A block is deliberately narrow: it hides what a person writes, not what the
school publishes. Blocking a teacher must not hide the announcement that the
exam moved, and a family that mutes the office by accident would find out too
late. So posts — which only staff may write, and which the school stands
behind — stay visible. Comments and messages, the parts a student or parent
authors, are what a block removes.
"""

import frappe
from frappe.utils import cint, now

from match_schools.api.utils import (
	fail,
	ms_endpoint,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

ALL_ROLES = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)

CONTENT_TYPES = ("Comment", "Post", "Message")
REASONS = ("Abuse", "Harassment", "Spam", "Inappropriate", "Privacy", "Other")
MAX_DETAILS = 1000
SNAPSHOT_CHARS = 2000

REASON_AR = {
	"Abuse": "إساءة أو لغة مسيئة",
	"Harassment": "تحرّش أو تنمّر",
	"Spam": "رسائل مزعجة أو دعائية",
	"Inappropriate": "محتوى غير لائق",
	"Privacy": "انتهاك خصوصية",
	"Other": "سبب آخر",
}


# ---------------------------------------------------------------- blocks


def hidden_authors(user: str | None = None) -> set[str]:
	"""Everyone whose writing must not reach `user`.

	Both directions: the people they blocked, and the people who blocked them.
	The second half matters more than it looks — without it a blocked person
	still sees their target's replies and can answer them, which is the
	conversation the block was meant to end.
	"""
	user = user or frappe.session.user
	rows = frappe.get_all(
		"MS User Block",
		or_filters=[["user", "=", user], ["blocked_user", "=", user]],
		fields=["user", "blocked_user"],
		limit_page_length=0,
	)
	out: set[str] = set()
	for r in rows:
		out.add(r.blocked_user if r.user == user else r.user)
	out.discard(user)
	return out


def blocked_between(sender: str, users: list[str]) -> set[str]:
	"""Of `users`, those who must not receive anything from `sender`.

	The per-user form would be one query per recipient, and a staff message
	may carry two hundred; this asks once.
	"""
	users = [u for u in dict.fromkeys(users or []) if u and u != sender]
	if not users:
		return set()
	rows = frappe.get_all(
		"MS User Block",
		or_filters=[
			["user", "in", users],
			["blocked_user", "in", users],
		],
		fields=["user", "blocked_user"],
		limit_page_length=0,
	)
	targets = set(users)
	out: set[str] = set()
	for r in rows:
		if r.blocked_user == sender and r.user in targets:
			out.add(r.user)          # they blocked the sender
		elif r.user == sender and r.blocked_user in targets:
			out.add(r.blocked_user)  # the sender blocked them
	return out


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def block_user(user: str = None, reason: str = None, persona: str = None):
	"""Stop seeing what this person writes, and stop them seeing mine."""
	me = frappe.session.user
	if not user:
		return fail(message_en="A user is required.", message_ar="يجب تحديد المستخدم.")
	if user == me:
		return fail(
			message_en="You cannot block yourself.",
			message_ar="لا يمكنك حظر نفسك.",
		)
	if not frappe.db.exists("User", user):
		return fail(message_en="Unknown user.", message_ar="مستخدم غير معروف.")

	existing = frappe.db.exists("MS User Block", {"user": me, "blocked_user": user})
	if not existing:
		doc = frappe.get_doc(
			{
				"doctype": "MS User Block",
				"user": me,
				"blocked_user": user,
				"blocked_user_name": frappe.db.get_value("User", user, "full_name") or user,
				"blocked_on": now(),
				"reason": (reason or "")[:MAX_DETAILS],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

	return {
		"blocked": True,
		"user": user,
		"message_en": "Blocked. You will no longer see this person's comments or messages.",
		"message_ar": "تم الحظر. لن تظهر لك تعليقات هذا الشخص ولا رسائله.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def unblock_user(user: str = None, persona: str = None):
	me = frappe.session.user
	if not user:
		return fail(message_en="A user is required.", message_ar="يجب تحديد المستخدم.")
	for name in frappe.get_all(
		"MS User Block", filters={"user": me, "blocked_user": user}, pluck="name"
	):
		frappe.delete_doc("MS User Block", name, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {
		"blocked": False,
		"user": user,
		"message_en": "Unblocked.",
		"message_ar": "تم رفع الحظر.",
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def my_blocks(persona: str = None):
	"""The people this caller has blocked, for the settings screen."""
	rows = frappe.get_all(
		"MS User Block",
		filters={"user": frappe.session.user},
		fields=["blocked_user", "blocked_user_name", "blocked_on", "reason"],
		order_by="blocked_on desc",
		limit_page_length=0,
	)
	return {
		"blocks": [
			{
				"user": r.blocked_user,
				"name": r.blocked_user_name
				or frappe.db.get_value("User", r.blocked_user, "full_name")
				or r.blocked_user,
				"blocked_on": str(r.blocked_on or ""),
				"reason": r.reason,
			}
			for r in rows
		]
	}


# --------------------------------------------------------------- reports


def _subject(content_type: str, reference: str) -> tuple[str | None, str | None, str]:
	"""Who wrote the reported thing, and a copy of what it said.

	Returns (author, author_name, snapshot). Reading it here — rather than
	trusting what the app sends — is what keeps a crafted request from filing
	a report against someone who never wrote it.
	"""
	if content_type == "Comment":
		row = frappe.db.get_value(
			"MS Post Comment", reference, ["author", "author_name", "body"], as_dict=True
		)
		if not row:
			return None, None, ""
		return row.author, row.author_name, (row.body or "")[:SNAPSHOT_CHARS]

	if content_type == "Post":
		row = frappe.db.get_value(
			"MS Community Post", reference, ["owner", "author_name", "title", "body"], as_dict=True
		)
		if not row:
			return None, None, ""
		text = f"{row.title or ''}\n{row.body or ''}".strip()
		return row.owner, row.author_name, text[:SNAPSHOT_CHARS]

	row = frappe.db.get_value(
		"MS Message", reference, ["sender", "sender_name", "subject", "body"], as_dict=True
	)
	if not row:
		return None, None, ""
	text = f"{row.subject or ''}\n{row.body or ''}".strip()
	return row.sender, row.sender_name, text[:SNAPSHOT_CHARS]


def _may_report(content_type: str, reference: str) -> bool:
	"""Only someone who can legitimately see the content may report it.

	Otherwise the report form becomes a way to read a comment on a post meant
	for another family, by reporting it and reading the copy in the record.
	"""
	me = frappe.session.user
	if content_type == "Message":
		if frappe.db.get_value("MS Message", reference, "sender") == me:
			return True
		return bool(
			frappe.db.exists("MS Message Recipient", {"message": reference, "user": me})
		)

	from match_schools.api.community import _may_see

	post = (
		reference
		if content_type == "Post"
		else frappe.db.get_value("MS Post Comment", reference, "post")
	)
	if not post or not frappe.db.exists("MS Community Post", post):
		return False
	from match_schools.api.utils import get_persona

	return _may_see(get_persona(), frappe.get_doc("MS Community Post", post))


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def report_content(
	content_type: str = None,
	reference: str = None,
	reason: str = None,
	details: str = None,
	persona: str = None,
):
	"""File a report with the school office about a piece of content."""
	me = frappe.session.user
	content_type = (content_type or "").strip().title()
	reason = (reason or "Other").strip().title()

	if content_type not in CONTENT_TYPES:
		return fail(message_en="Unknown content type.", message_ar="نوع محتوى غير معروف.")
	if not reference:
		return fail(message_en="A reference is required.", message_ar="يجب تحديد المحتوى.")
	if reason not in REASONS:
		reason = "Other"
	if not _may_report(content_type, reference):
		return fail(
			message_en="This content is not available to you.",
			message_ar="هذا المحتوى غير متاح لك.",
		)

	author, author_name, snapshot = _subject(content_type, reference)
	if author is None:
		return fail(
			message_en="This content no longer exists.",
			message_ar="هذا المحتوى لم يعد موجوداً.",
		)

	# A second report on the same thing by the same person is the same report.
	existing = frappe.db.exists(
		"MS Content Report",
		{"content_type": content_type, "reference": reference, "reported_by": me},
	)
	if existing:
		return {
			"report": existing,
			"already": True,
			"message_en": "You have already reported this. The school has it.",
			"message_ar": "سبق أن أبلغت عن هذا المحتوى، والبلاغ عند إدارة المدرسة.",
		}

	doc = frappe.get_doc(
		{
			"doctype": "MS Content Report",
			"content_type": content_type,
			"reference": reference,
			"reported_user": author,
			"reported_user_name": author_name,
			"reported_by": me,
			"reported_by_name": frappe.db.get_value("User", me, "full_name") or me,
			"reported_on": now(),
			"reason": reason,
			"details": (details or "")[:MAX_DETAILS],
			"snapshot": snapshot,
			"status": "New",
		}
	)
	doc.insert(ignore_permissions=True)
	_notify_office(doc)
	frappe.db.commit()

	return {
		"report": doc.name,
		"already": False,
		"message_en": "Report received. The school reviews reports within 24 hours.",
		"message_ar": "تم استلام البلاغ. تراجع إدارة المدرسة البلاغات خلال ٢٤ ساعة.",
	}


def _notify_office(doc) -> None:
	"""Put a new report in front of the office rather than in a list they may
	never open. A moderation promise the school cannot see is not a promise."""
	from match_schools import push

	admins = frappe.get_all(
		"Has Role",
		filters={
			"role": ["in", ["MS School Admin", "MS Secretary"]],
			"parenttype": "User",
		},
		pluck="parent",
		limit_page_length=0,
	)
	recipients = sorted(
		{
			u
			for u in admins
			if u not in ("Administrator", "Guest")
			and frappe.db.get_value("User", u, "enabled")
		}
	)
	if not recipients:
		return
	try:
		push.notify(
			recipients,
			"بلاغ عن محتوى",
			f"{doc.reported_by_name}: {REASON_AR.get(doc.reason, doc.reason)}",
			channel="alerts",
			type="content_report",
			id=f"content-report:{doc.name}",
			ref=doc.name,
		)
	except Exception:
		# A failed notification must not lose the report itself.
		frappe.log_error(frappe.get_traceback(), "moderation._notify_office")


@frappe.whitelist()
@ms_endpoint(*STAFF)
def reports(status: str = None, limit: int = 50, persona: str = None):
	"""The moderation queue, for the office."""
	filters: dict = {}
	if status:
		filters["status"] = status
	rows = frappe.get_all(
		"MS Content Report",
		filters=filters,
		fields=[
			"name", "content_type", "reference", "reported_user", "reported_user_name",
			"reported_by", "reported_by_name", "reported_on", "reason", "details",
			"snapshot", "status", "handled_by", "handled_on", "resolution",
		],
		order_by="reported_on desc",
		limit_page_length=min(max(cint(limit) or 50, 1), 200),
	)
	for r in rows:
		r["reason_label"] = REASON_AR.get(r["reason"], r["reason"])
		r["reported_on"] = str(r["reported_on"] or "")
		r["handled_on"] = str(r["handled_on"] or "")
	# Unhandled first; the query already returned newest-first, and a stable
	# sort keeps that order inside each group.
	rows.sort(key=lambda r: r["status"] != "New")
	return {
		"reports": rows,
		"open_count": frappe.db.count("MS Content Report", {"status": "New"}),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def resolve_report(
	report: str = None, status: str = None, resolution: str = None, persona: str = None
):
	"""Record what the school did about a report."""
	if not report or not frappe.db.exists("MS Content Report", report):
		return fail(message_en="Unknown report.", message_ar="بلاغ غير معروف.")
	status = (status or "Reviewed").strip()
	if status not in ("Reviewed", "Content removed", "User warned", "Dismissed"):
		return fail(message_en="Unknown status.", message_ar="حالة غير معروفة.")

	doc = frappe.get_doc("MS Content Report", report)
	doc.status = status
	doc.handled_by = frappe.session.user
	doc.handled_on = now()
	doc.resolution = (resolution or "")[:MAX_DETAILS]
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"report": doc.name, "status": doc.status}
