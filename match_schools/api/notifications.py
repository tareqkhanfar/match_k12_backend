# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""A unified notification feed.

Rather than keeping a separate notification table that has to be written to on
every event, the feed is derived on read from what already exists: unread
messages, fresh announcements, assignments falling due, newly published marks,
absences and overdue fees. That way it can never drift out of sync with the
records it describes.
"""

import frappe
import frappe.defaults
from frappe.utils import add_days, cint, flt, now, now_datetime, today

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	ms_endpoint,
	ok,
	resolve_scope,
)

# How far back a "recent" event stays in the feed.
LOOKBACK_DAYS = 14

# Ordering weight — lower sorts first when two events share a timestamp.
SEVERITY_RANK = {"danger": 0, "warning": 1, "info": 2, "success": 3}


READ_STATE_KEY = "ms_notifications_read"

# Keep the dismissal record from growing without bound; anything older than the
# lookback window can no longer appear in the feed anyway.
MAX_READ_IDS = 500


def _read_state() -> dict:
	"""Which notification ids this user has dismissed.

	Stored as a per-user Frappe setting. `set_default` writes a *global*
	default, so the user-scoped `set_user_default` pair is used instead —
	otherwise one user's dismissals would hide notifications for everyone.
	"""
	raw = frappe.db.get_value(
		"DefaultValue",
		{"parent": frappe.session.user, "defkey": READ_STATE_KEY},
		"defvalue",
	)
	if not raw:
		return {}
	try:
		return frappe.parse_json(raw) or {}
	except Exception:
		return {}


def _save_read_state(state: dict):
	if len(state) > MAX_READ_IDS:
		state = dict(list(state.items())[-MAX_READ_IDS:])
	frappe.defaults.set_user_default(READ_STATE_KEY, frappe.as_json(state))


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def feed(limit: int = 30, unread_only: int = 0, persona: str = None):
	"""Everything the caller should know about, newest first."""
	return _build_feed(persona, limit=limit, unread_only=unread_only)


def _build_feed(persona: str, limit: int = 30, unread_only: int = 0) -> dict:
	"""The feed itself. Undecorated, so callers inside this module get raw data."""
	limit = min(max(cint(limit) or 30, 1), 100)
	scope = resolve_scope(persona)
	since = add_days(today(), -LOOKBACK_DAYS)

	items: list[dict] = []
	items += _message_items()
	items += _announcement_items(persona, since)

	if persona == ROLE_STUDENT:
		student = scope.get("student")
		if student:
			items += _student_items(student, since)
	elif persona == ROLE_PARENT:
		for student in scope.get("students") or []:
			items += _student_items(student, since, prefix_name=True, for_guardian=True)
	elif persona == ROLE_TEACHER:
		items += _teacher_items(scope, since)
	elif persona in BACK_OFFICE:
		items += _back_office_items(since)

	read = _read_state()
	for it in items:
		it["read"] = bool(read.get(it["id"])) or it.get("read", False)

	if cint(unread_only):
		items = [i for i in items if not i["read"]]

	items.sort(
		key=lambda i: (str(i.get("time") or ""), -SEVERITY_RANK.get(i.get("tone"), 9)),
		reverse=True,
	)
	trimmed = items[:limit]

	return {
		"items": trimmed,
		"unread": sum(1 for i in items if not i["read"]),
		"total": len(items),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def unread_count(persona: str = None):
	"""Just the badge number — cheap enough to poll."""
	return {"unread": _build_feed(persona, limit=100)["unread"]}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def mark_read(notification: str = None, all: int = 0, persona: str = None):
	"""Dismiss one notification, or every one currently in the feed."""
	state = _read_state()

	if cint(all):
		for item in _build_feed(persona, limit=100)["items"]:
			state[item["id"]] = 1
		# Messages carry their own read flag, so clear that too.
		frappe.db.set_value(
			"MS Message",
			{"recipient": frappe.session.user, "read_by_recipient": 0},
			"read_by_recipient",
			1,
			update_modified=False,
		)
	elif notification:
		state[notification] = 1
		if notification.startswith("msg:"):
			thread = notification.split(":", 1)[1]
			frappe.db.set_value(
				"MS Message",
				{"thread": thread, "recipient": frappe.session.user, "read_by_recipient": 0},
				"read_by_recipient",
				1,
				update_modified=False,
			)

	_save_read_state(state)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"read": len(state)},
		"message_en": "Marked as read.",
		"message_ar": "تم وضع علامة مقروء.",
	}


# --- Item builders ---------------------------------------------------------


def _message_items() -> list[dict]:
	"""Unread direct messages, grouped by conversation."""
	rows = frappe.get_all(
		"MS Message",
		filters={"recipient": frappe.session.user, "read_by_recipient": 0},
		fields=["name", "thread", "subject", "body", "sender", "sent_on"],
		order_by="sent_on desc",
		limit=30,
	)
	by_thread: dict[str, dict] = {}
	for r in rows:
		if r.thread in by_thread:
			by_thread[r.thread]["count"] += 1
			continue
		by_thread[r.thread] = {
			"id": f"msg:{r.thread}",
			"category": "message",
			"category_label": "رسالة",
			"title": _sender_name(r.sender),
			"body": frappe.utils.strip_html(r.subject or r.body or "")[:120],
			"time": str(r.sent_on or ""),
			"tone": "info",
			"link": "/app/communication",
			"ref": r.thread,
			"count": 1,
			"read": False,
		}
	return list(by_thread.values())


def _sender_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


def _announcement_items(persona: str, since: str) -> list[dict]:
	"""Recent announcements, reusing the audience rules from communication."""
	from match_schools.api.communication import list_announcements

	# list_announcements is a decorated endpoint, so it returns an envelope.
	envelope = list_announcements(limit=15, persona=persona)
	rows = envelope.get("data") if isinstance(envelope, dict) else envelope

	items = []
	for a in rows or []:
		if str(a.get("date") or "") < since:
			continue
		items.append(
			{
				"id": f"ann:{a['id']}",
				"category": "announcement",
				"category_label": "إعلان",
				"title": a["title"],
				"body": frappe.utils.strip_html(a.get("body") or "")[:120],
				"time": str(a.get("date") or ""),
				"tone": "danger" if a.get("type_raw") == "Alert" else "info",
				"link": "/app/communication",
				"ref": a["id"],
			}
		)
	return items


def _student_items(
	student: str, since: str, prefix_name: bool = False, for_guardian: bool = False
) -> list[dict]:
	"""Assignments due, new marks, absences and overdue fees for one student."""
	items: list[dict] = []
	name = ""
	if prefix_name:
		name = frappe.db.get_value("Student", student, "student_name") or ""

	def label(text: str) -> str:
		return f"{name}: {text}" if name else text

	# Assignments falling due in the next week that are still unsubmitted.
	groups = [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	]
	if groups:
		from match_schools.api.assignments import assignments_for_groups, handed_in_pairs

		horizon = add_days(today(), 7)
		# Read through the helper: it counts every section a piece of homework
		# was set for, and leaves out drafts and work still waiting for its
		# publish time — neither of which has reached this family.
		upcoming = assignments_for_groups(
			groups, {"status": "Open", "due_date": ["between", [today(), horizon]]}
		)
		upcoming.sort(key=lambda a: str(a.get("due_date") or ""))
		done = handed_in_pairs([a.name for a in upcoming])

		for a in upcoming[:15]:
			# "Handed in", not "has a row": a row now exists from the moment
			# the pupil opens the work, and treating that as done would drop
			# the reminder for exactly the child who needs it.
			if (a.name, student) in done:
				continue
			days_left = frappe.utils.date_diff(a.due_date, today())
			items.append(
				{
					"id": f"asg:{a.name}:{student}",
					"category": "assignment",
					"category_label": "واجب",
					"title": label(a.title),
					"body": (
						f"{a.course} — يستحق اليوم"
						if days_left == 0
						else f"{a.course} — متبقٍ {days_left} يوم"
					),
					"time": str(a.due_date),
					"tone": "danger" if days_left <= 1 else "warning",
					"link": "/app/assignments",
					"ref": a.name,
				}
			)

	# Homework set recently, whenever it is due. The reminder above only fires
	# in the last week before the deadline, so a piece of work set today and
	# due in three weeks reached nobody — which is the opposite of "it must
	# reach the pupil and their guardian".
	if groups:
		from match_schools.api.assignments import assignments_for_groups, handed_in_pairs

		fresh = [
			a
			for a in assignments_for_groups(groups)
			if str(a.get("assigned_on") or "") >= since
		]
		done = handed_in_pairs([a.name for a in fresh])
		for a in sorted(fresh, key=lambda x: str(x.get("assigned_on") or ""), reverse=True)[:10]:
			if (a.name, student) in done:
				continue
			# The teacher decides whether the family is told as well as the
			# pupil; the switch on the homework is what says so.
			if for_guardian and not cint(a.get("notify_guardians")):
				continue
			items.append(
				{
					"id": f"asgnew:{a.name}:{student}",
					"category": "assignment",
					"category_label": "واجب جديد",
					"title": label(f"واجب جديد: {a.title}"),
					"body": f"{a.course} — يستحق {a.due_date}",
					"time": str(a.get("assigned_on") or a.due_date),
					"tone": "info",
					"link": "/app/assignments",
					"ref": a.name,
				}
			)

	# Lessons called off. A cancelled period is the one timetable change a
	# family must be told about — otherwise a child turns up for a lesson that
	# is not happening, or waits at home for one that is.
	if groups:
		for c in frappe.get_all(
			"MS Lesson Change",
			filters={
				"student_group": ["in", groups],
				"change_type": "Cancelled",
				"docstatus": 1,
				"schedule_date": [">=", today()],
			},
			fields=["name", "course", "schedule_date", "reason", "notes", "modified"],
			order_by="schedule_date",
			limit=10,
		):
			items.append(
				{
					"id": f"cancel:{c.name}:{student}",
					"category": "timetable",
					"category_label": "إلغاء حصة",
					"title": label(f"أُلغيت حصة {c.course}"),
					"body": " — ".join(
						p for p in (str(c.schedule_date), c.reason or c.notes) if p
					),
					"time": str(c.modified),
					"tone": "danger",
					"link": "/app/timetable",
					"ref": c.name,
				}
			)

	# Surveys open to this student. Writing one and opening it used to notify
	# nobody, so a survey aimed at families reached only whoever happened to
	# visit the surveys page while it was still open.
	for s in frappe.get_all(
		"MS Survey",
		filters={
			"status": "Open",
			"audience": ["in", ["Students", "All"]],
		},
		fields=["name", "title", "closes_on", "modified"],
		order_by="modified desc",
		limit=10,
	):
		# Responses are recorded against the user who answered, not the
		# student, so an already-answered survey stops nagging its respondent.
		student_user = frappe.db.get_value("Student", student, "user")
		if student_user and frappe.db.exists(
			"MS Survey Response", {"survey": s.name, "respondent": student_user}
		):
			continue
		items.append(
			{
				"id": f"survey:{s.name}:{student}",
				"category": "survey",
				"category_label": "استبيان",
				"title": label(s.title),
				"body": (
					f"يغلق في {str(s.closes_on)[:10]}" if s.closes_on else "بانتظار رأيك"
				),
				"time": str(s.modified),
				"tone": "info",
				"link": "/app/surveys",
				"ref": s.name,
			}
		)

	# Marks entered recently.
	for g in frappe.get_all(
		"MS Gradebook Entry",
		filters={"student": student, "modified": [">=", since]},
		fields=["name", "course", "component_name", "score", "max_score", "modified"],
		order_by="modified desc",
		limit=10,
	):
		pct = (flt(g.score) / flt(g.max_score) * 100) if flt(g.max_score) else 0
		items.append(
			{
				"id": f"grd:{g.name}",
				"category": "grade",
				"category_label": "علامة",
				"title": label(f"{g.course} — {g.component_name}"),
				"body": f"{flt(g.score):g} من {flt(g.max_score):g} ({pct:.0f}%)",
				"time": str(g.modified or ""),
				"tone": "success" if pct >= 75 else "warning" if pct >= 50 else "danger",
				"link": "/app/record",
				"ref": g.name,
			}
		)

	# Absences.
	for att in frappe.get_all(
		"Student Attendance",
		filters={"student": student, "status": "Absent", "date": [">=", since], "docstatus": 1},
		fields=["name", "date"],
		order_by="date desc",
		limit=5,
	):
		items.append(
			{
				"id": f"att:{att.name}",
				"category": "attendance",
				"category_label": "غياب",
				"title": label("تسجيل غياب"),
				"body": f"تم تسجيل غياب بتاريخ {att.date}",
				"time": str(att.date),
				"tone": "warning",
				"link": "/app/attendance",
				"ref": att.name,
			}
		)

	# Fees past their due date.
	for fee in frappe.get_all(
		"Sales Invoice",
		filters={"student": student, "outstanding_amount": [">", 0], "docstatus": 1},
		fields=["name", "outstanding_amount", "due_date"],
		order_by="due_date",
		limit=5,
	):
		if fee.due_date and str(fee.due_date) >= today():
			continue
		items.append(
			{
				"id": f"fee:{fee.name}",
				"category": "fee",
				"category_label": "رسوم",
				"title": label("رسوم متأخرة"),
				"body": f"مبلغ مستحق {flt(fee.outstanding_amount):g} — تاريخ الاستحقاق {fee.due_date}",
				"time": str(fee.due_date or today()),
				"tone": "danger",
				"link": "/app/fees",
				"ref": fee.name,
			}
		)

	return items


def _teacher_items(scope: dict, since: str) -> list[dict]:
	"""Submissions waiting to be marked, and assignments closing soon."""
	instructor = scope.get("instructor")
	if not instructor:
		return []

	items = []

	# Lessons of theirs that were called off, so a teacher is not the last to
	# know their own period is not running.
	for c in frappe.get_all(
		"MS Lesson Change",
		filters={
			"original_instructor": instructor,
			"change_type": "Cancelled",
			"docstatus": 1,
			"schedule_date": [">=", today()],
		},
		fields=["name", "course", "student_group", "schedule_date", "reason", "notes", "modified"],
		order_by="schedule_date",
		limit=10,
	):
		items.append(
			{
				"id": f"cancel:{c.name}",
				"category": "timetable",
				"category_label": "إلغاء حصة",
				"title": f"أُلغيت حصة {c.course} — {c.student_group}",
				"body": " — ".join(
					p for p in (str(c.schedule_date), c.reason or c.notes) if p
				),
				"time": str(c.modified),
				"tone": "danger",
				"link": "/app/timetable",
				"ref": c.name,
			}
		)

	# Surveys aimed at staff.
	for s in frappe.get_all(
		"MS Survey",
		filters={"status": "Open", "audience": ["in", ["Teachers", "All"]]},
		fields=["name", "title", "closes_on", "modified"],
		order_by="modified desc",
		limit=10,
	):
		if frappe.db.exists(
			"MS Survey Response", {"survey": s.name, "respondent": frappe.session.user}
		):
			continue
		items.append(
			{
				"id": f"survey:{s.name}",
				"category": "survey",
				"category_label": "استبيان",
				"title": s.title,
				"body": f"يغلق في {str(s.closes_on)[:10]}" if s.closes_on else "بانتظار رأيك",
				"time": str(s.modified),
				"tone": "info",
				"link": "/app/surveys",
				"ref": s.name,
			}
		)

	assignments = frappe.get_all(
		"MS Assignment",
		filters={"instructor": instructor, "status": ["in", ["Open", "Grading"]]},
		fields=["name", "title", "course", "due_date"],
		limit=30,
	)
	for a in assignments:
		pending = frappe.db.count(
			"MS Assignment Submission",
			{"assignment": a.name, "status": ["in", ["Submitted", "Late"]]},
		)
		if pending:
			items.append(
				{
					"id": f"grade-todo:{a.name}",
					"category": "grading",
					"category_label": "تصحيح",
					"title": a.title,
					"body": f"{pending} تسليم بانتظار التصحيح — {a.course}",
					"time": str(a.due_date or today()),
					"tone": "warning",
					"link": "/app/assignments",
					"ref": a.name,
				}
			)
	return items


def _back_office_items(since: str) -> list[dict]:
	"""School-wide signals for the admin and secretary."""
	items = []

	absent_today = frappe.db.count(
		"Student Attendance", {"status": "Absent", "date": today(), "docstatus": 1}
	)
	if absent_today:
		items.append(
			{
				"id": f"abs-today:{today()}",
				"category": "attendance",
				"category_label": "غياب",
				"title": "غياب اليوم",
				"body": f"{absent_today} طالبًا مسجلون كغائبين اليوم",
				"time": str(now_datetime()),
				"tone": "warning",
				"link": "/app/attendance",
				"ref": today(),
			}
		)

	overdue = frappe.db.sql(
		"""
		SELECT COUNT(*) AS count, COALESCE(SUM(outstanding_amount), 0) AS total
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND IFNULL(student, '') != ''
		  AND outstanding_amount > 0 AND due_date < %(today)s
		""",
		{"today": today()},
		as_dict=True,
	)
	if overdue and overdue[0].count:
		items.append(
			{
				"id": f"fees-overdue:{today()}",
				"category": "fee",
				"category_label": "رسوم",
				"title": "رسوم متأخرة",
				"body": f"{overdue[0].count} فاتورة متأخرة بقيمة {flt(overdue[0].total):g}",
				"time": str(now_datetime()),
				"tone": "danger",
				"link": "/app/fees",
				"ref": "overdue",
			}
		)

	return items


# ---------------------------------------------------------------------------
# إشعارات الدفع
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def register_device(token: str = None, platform: str = "android", persona: str = None):
	"""تسجيل جهاز ليتلقّى إشعارات الدفع.

	الرمز يخصّ الجهاز لا الشخص، فهاتف يتناوب عليه اثنان يصل رمزه نفسه. لذلك
	أي تسجيل سابق للرمز نفسه — لأي مستخدم — يُحوَّل إلى صاحب الجلسة الحالية
	بدل أن يُضاف بجانبه: وإلا وصلت إشعارات الأول إلى الثاني.
	"""
	token = (token or "").strip()
	if not token:
		return fail(message_en="A device token is required.", message_ar="رمز الجهاز مطلوب.")
	if len(token) > 512:
		return fail(message_en="Token is too long.", message_ar="رمز الجهاز طويل جداً.")

	existing = frappe.get_all("MS Device Token", filters={"token": token}, pluck="name")
	if existing:
		for name in existing:
			frappe.db.set_value(
				"MS Device Token",
				name,
				{
					"user": frappe.session.user,
					"platform": platform or "android",
					"is_active": 1,
					"failures": 0,
					"last_seen": now(),
				},
				update_modified=False,
			)
	else:
		frappe.get_doc(
			{
				"doctype": "MS Device Token",
				"user": frappe.session.user,
				"token": token,
				"platform": platform or "android",
				"is_active": 1,
				"last_seen": now(),
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()
	return ok(
		{"registered": True},
		message_en="Device registered.",
		message_ar="تم تسجيل الجهاز للإشعارات.",
	)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def unregister_device(token: str = None, persona: str = None):
	"""إلغاء تسجيل جهاز — يُستدعى عند تسجيل الخروج."""
	token = (token or "").strip()
	if not token:
		return fail(message_en="A device token is required.", message_ar="رمز الجهاز مطلوب.")

	for name in frappe.get_all(
		"MS Device Token",
		filters={"token": token, "user": frappe.session.user},
		pluck="name",
	):
		frappe.delete_doc("MS Device Token", name, ignore_permissions=True, force=True)

	frappe.db.commit()
	return ok(
		{},
		message_en="Device unregistered.",
		message_ar="تم إلغاء تسجيل الجهاز.",
	)


def tokens_for(users: list[str]) -> list[str]:
	"""رموز الأجهزة الفعّالة لمجموعة مستخدمين."""
	if not users:
		return []
	return frappe.get_all(
		"MS Device Token",
		filters={"user": ["in", users], "is_active": 1},
		pluck="token",
		limit_page_length=0,
	)
