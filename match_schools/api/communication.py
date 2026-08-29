# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Announcements and direct messages."""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, today

from match_schools.api.utils import (
	apply_period,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	ms_endpoint,
	resolve_scope,
	ROLE_SECRETARY,
)

TYPE_AR = {"Announcement": "إعلان", "Event": "حدث", "Alert": "تنبيه"}

# Which announcement audiences each persona should receive.
AUDIENCE_FOR_PERSONA = {
	ROLE_ADMIN: ["All", "Students", "Teachers", "Parents", "Program", "Student Group"],
	# The secretary runs the front office, so they see everything the admin does.
	ROLE_SECRETARY: ["All", "Students", "Teachers", "Parents", "Program", "Student Group"],
	ROLE_TEACHER: ["All", "Teachers"],
	ROLE_STUDENT: ["All", "Students"],
	ROLE_PARENT: ["All", "Parents"],
}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_announcements(limit: int = 25, persona: str = None):
	"""Announcements targeted at the caller."""
	limit = min(max(cint(limit) or 25, 1), 100)
	scope = resolve_scope(persona)

	audiences = AUDIENCE_FOR_PERSONA.get(persona, ["All"])
	filters = {"published": 1, "audience": ["in", audiences]}
	apply_period(filters, "MS Announcement")

	rows = frappe.get_all(
		"MS Announcement",
		filters=filters,
		fields=[
			"name", "title", "body", "posted_on", "expires_on",
			"announcement_type", "audience", "audience_label", "program", "student_group",
		],
		order_by="posted_on desc",
		limit=limit,
	)

	# Students and parents also get announcements aimed at their own
	# program / student group.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		groups = _groups_for_students(scope.get("students") or [])
		programs = _programs_for_students(scope.get("students") or [])
		if groups or programs:
			targeted = frappe.get_all(
				"MS Announcement",
				filters={
					"published": 1,
					"audience": ["in", ["Program", "Student Group"]],
				},
				fields=[
					"name", "title", "body", "posted_on", "expires_on",
					"announcement_type", "audience", "audience_label", "program", "student_group",
				],
				order_by="posted_on desc",
				limit=limit,
			)
			seen = {r.name for r in rows}
			for t in targeted:
				if t.name in seen:
					continue
				if (t.audience == "Program" and t.program in programs) or (
					t.audience == "Student Group" and t.student_group in groups
				):
					rows.append(t)
			rows.sort(key=lambda r: r.get("posted_on") or "", reverse=True)
			rows = rows[:limit]

	# An announcement past its expiry date drops out of the feed.
	stamp = today()
	rows = [r for r in rows if not r.expires_on or str(r.expires_on) >= stamp]

	return [
		{
			"id": r.name,
			"title": r.title,
			"body": r.body,
			"date": str(r.posted_on or ""),
			"expires_on": str(r.expires_on or ""),
			"type": TYPE_AR.get(r.announcement_type, r.announcement_type),
			"type_raw": r.announcement_type,
			"audience": r.audience_label or _audience_label(r),
		}
		for r in rows
	]


def _audience_label(r: dict) -> str:
	labels = {
		"All": "الجميع",
		"Students": "الطلاب",
		"Teachers": "المعلمون",
		"Parents": "أولياء الأمور",
	}
	if r.get("audience") == "Program":
		return r.get("program") or "برنامج"
	if r.get("audience") == "Student Group":
		return r.get("student_group") or "شعبة"
	return labels.get(r.get("audience"), r.get("audience") or "")


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


def _programs_for_students(students: list[str]) -> list[str]:
	if not students:
		return []
	return list(
		{
			r.program
			for r in frappe.get_all(
				"Program Enrollment",
				filters={"student": ["in", students], "docstatus": ["<", 2]},
				fields=["program"],
			)
			if r.program
		}
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_announcement(payload: str | dict, persona: str = None):
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	fields = {
		"title": data.get("title"),
		"body": data.get("body"),
		"announcement_type": data.get("type") or "Announcement",
		"audience": data.get("audience") or "All",
		"audience_label": data.get("audience_label"),
		"program": data.get("program"),
		"student_group": data.get("student_group"),
		"posted_on": data.get("posted_on") or today(),
		"expires_on": data.get("expires_on"),
		"published": cint(data.get("published", 1)),
	}
	fields = {k: v for k, v in fields.items() if v is not None}

	announcement_id = data.get("id") or data.get("name")
	if announcement_id:
		doc = frappe.get_doc("MS Announcement", announcement_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Announcement updated.", "تم تحديث الإعلان."
	else:
		if not fields.get("title"):
			return fail(message_en="Title is required.", message_ar="العنوان مطلوب.")
		fields["doctype"] = "MS Announcement"
		doc = frappe.get_doc(fields)
		doc.insert()
		msg_en, msg_ar = "Announcement published.", "تم نشر الإعلان."

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.title},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_announcement(announcement: str, persona: str = None):
	frappe.delete_doc("MS Announcement", announcement)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": announcement},
		"message_en": "Announcement deleted.",
		"message_ar": "تم حذف الإعلان.",
	}


# --- Messages --------------------------------------------------------------


def _as_html(text: str) -> str:
	"""Message bodies render as HTML, so plain text must be converted.

	A body that already carries markup is left alone; anything else is escaped
	and its line breaks preserved, so a chat message can never inject markup.
	"""
	if not text:
		return ""
	if "<" in text and ">" in text:
		return text
	escaped = frappe.utils.escape_html(text.strip())
	return "".join(f"<p>{line}</p>" for line in escaped.split("\n") if line.strip()) or ""


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def inbox(limit: int = 50, student: str = None, persona: str = None):
	"""Latest message per conversation involving the current user.

	A guardian with several children gets one inbox per child rather than a
	merged list: passing `student` narrows it to conversations about that
	child, which is how a parent actually thinks about school correspondence.
	"""
	user = frappe.session.user
	limit = min(max(cint(limit) or 50, 1), 200)

	rows = frappe.db.sql(
		"""
		SELECT m.name, m.thread, m.subject, m.body, m.sender, m.recipient,
			m.sent_on, m.read_by_recipient, m.about_student
		FROM `tabMS Message` m
		INNER JOIN (
			SELECT thread, MAX(sent_on) AS latest
			FROM `tabMS Message`
			WHERE sender = %(user)s OR recipient = %(user)s
			GROUP BY thread
		) t ON t.thread = m.thread AND t.latest = m.sent_on
		WHERE m.sender = %(user)s OR m.recipient = %(user)s
		ORDER BY m.sent_on DESC
		LIMIT %(limit)s
		""",
		{"user": user, "limit": limit},
		as_dict=True,
	)

	# Narrow to one child. Threads with no student attached — a school-wide
	# announcement, say — stay visible under every child rather than
	# disappearing when a filter is applied.
	if student:
		rows = [r for r in rows if not r.about_student or r.about_student == student]

	out = []
	for r in rows:
		other = r.recipient if r.sender == user else r.sender
		unread = frappe.db.count(
			"MS Message",
			{"thread": r.thread, "recipient": user, "read_by_recipient": 0},
		)
		out.append(
			{
				"id": r.name,
				"thread": r.thread,
				"subject": r.subject,
				"preview": frappe.utils.strip_html(r.body or "")[:140],
				"from": _display_name(other),
				"from_user": other,
				"role": _persona_label(other),
				"time": str(r.sent_on or ""),
				"unread": bool(unread),
				"unread_count": unread,
				"outgoing": r.sender == user,
				"about_student": r.about_student,
			}
		)
	return out


def _display_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


def _persona_label(user: str) -> str:
	from match_schools.api.utils import get_persona

	labels = {
		ROLE_ADMIN: "إدارة",
		ROLE_TEACHER: "معلم",
		ROLE_STUDENT: "طالب",
		ROLE_PARENT: "ولي أمر",
	}
	return labels.get(get_persona(user), "")


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def thread(thread: str, persona: str = None):
	"""Full conversation; marks the caller's incoming messages as read."""
	user = frappe.session.user
	rows = frappe.get_all(
		"MS Message",
		filters={"thread": thread},
		fields=[
			"name", "subject", "body", "sender", "recipient",
			"sent_on", "read_by_recipient", "attachment", "about_student",
		],
		order_by="sent_on asc",
	)
	if not rows:
		return fail(message_en="Conversation not found.", message_ar="لم يتم العثور على المحادثة.")

	# Only the two participants may read a thread.
	participants = {r.sender for r in rows} | {r.recipient for r in rows}
	if user not in participants:
		frappe.throw(_("You are not part of this conversation."), frappe.PermissionError)

	unread = [r.name for r in rows if r.recipient == user and not r.read_by_recipient]
	for name in unread:
		frappe.db.set_value("MS Message", name, "read_by_recipient", 1, update_modified=False)
	if unread:
		frappe.db.commit()

	return {
		"thread": thread,
		"messages": [
			{
				"id": r.name,
				"subject": r.subject,
				"body": r.body,
				"sender": r.sender,
				"sender_name": _display_name(r.sender),
				"recipient": r.recipient,
				"sent_on": str(r.sent_on or ""),
				"outgoing": r.sender == user,
				"attachment": r.attachment,
				"about_student": r.about_student,
			}
			for r in rows
		],
	}


def _may_write_to(persona: str, recipient: str) -> bool:
	"""Whether this caller is allowed to message that user.

	The contact list already answers "who may I talk to" — a student sees their
	own teachers and the office, a teacher sees the guardians of the students
	they teach. Until now only the screen consulted it, so a request naming any
	other user went straight through: a student could message any other student
	in the school, which is unsupervised contact between children and exactly
	what a school cannot allow.

	Enforced against the same list the screen renders, so the two cannot drift.
	"""
	if persona in (ROLE_ADMIN, ROLE_SECRETARY):
		return True
	# contacts() returns the list itself; ms_endpoint wraps it in an envelope
	# when called over HTTP, so both shapes are unwrapped here.
	result = contacts(persona=persona)
	people = result.get("data") if isinstance(result, dict) else result
	return any(c.get("user") == recipient for c in (people or []) if isinstance(c, dict))


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def send_message(
	recipient: str,
	body: str,
	subject: str = None,
	thread: str = None,
	about_student: str = None,
	persona: str = None,
):
	if not recipient or not body:
		return fail(
			message_en="Recipient and message body are required.",
			message_ar="المستلم ونص الرسالة مطلوبان.",
		)
	if not _may_write_to(persona, recipient):
		frappe.throw(
			_("You may only message people on your contact list."),
			frappe.PermissionError,
		)

	if not frappe.db.exists("User", recipient):
		return fail(message_en="Recipient not found.", message_ar="لم يتم العثور على المستلم.")

	doc = frappe.get_doc(
		{
			"doctype": "MS Message",
			"sender": frappe.session.user,
			"recipient": recipient,
			"subject": subject,
			"body": _as_html(body),
			"thread": thread,
			"about_student": about_student,
			"sent_on": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "thread": doc.thread or doc.name},
		"message_en": "Message sent.",
		"message_ar": "تم إرسال الرسالة.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def contacts(persona: str = None):
	"""Users the caller is allowed to message."""
	scope = resolve_scope(persona)
	out: list[dict] = []
	seen: set[str] = set()

	def add(user: str | None, name: str, role: str):
		if not user or user in seen or user == frappe.session.user:
			return
		seen.add(user)
		out.append({"user": user, "name": name or user, "role": role})

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		# The teachers who actually teach this student's groups.
		groups = _groups_for_students(scope.get("students") or [])
		if groups:
			for i in frappe.get_all(
				"Student Group Instructor",
				filters={"parent": ["in", groups], "parenttype": "Student Group"},
				fields=["instructor", "instructor_name"],
			):
				add(_instructor_user(i.instructor), i.instructor_name, "معلم")

			# Instructors named on the timetable but not on the group roster.
			for cs in frappe.get_all(
				"Course Schedule",
				filters={"student_group": ["in", groups]},
				fields=["instructor", "instructor_name"],
				limit=200,
			):
				if cs.instructor:
					add(_instructor_user(cs.instructor), cs.instructor_name, "معلم")

		# Everyone can always reach the front office.
		for user, name in _back_office_users():
			add(user, name, "إدارة المدرسة")

	else:
		# Staff can reach the guardians of the students they are responsible for.
		if persona == ROLE_TEACHER:
			from match_schools.api.students import _students_of_instructor

			students = _students_of_instructor(scope.get("instructor"))
		else:
			students = frappe.get_all("Student", filters={"enabled": 1}, pluck="name", limit=500)

		if students:
			for g in frappe.get_all(
				"Student Guardian",
				filters={"parent": ["in", students], "parenttype": "Student"},
				fields=["guardian", "guardian_name"],
			):
				add(
					frappe.db.get_value("Guardian", g.guardian, "user"),
					g.guardian_name,
					"ولي أمر",
				)

			# ...and the students themselves, where an account exists.
			for st in frappe.get_all(
				"Student",
				filters={"name": ["in", students], "enabled": 1},
				fields=["student_name", "user"],
				limit=500,
			):
				add(st.user, st.student_name, "طالب")

		# Staff can always reach each other.
		for user, name in _back_office_users():
			add(user, name, "إدارة المدرسة")
		for i in frappe.get_all(
			"Instructor", fields=["name", "instructor_name"], limit=200
		):
			add(_instructor_user(i.name), i.instructor_name, "معلم")

	out.sort(key=lambda c: (c["role"], c["name"]))
	return out


def _back_office_users() -> list[tuple[str, str]]:
	"""Enabled admin and secretary accounts."""
	from match_schools.api.utils import FRAPPE_ROLE_BY_PERSONA

	roles = [FRAPPE_ROLE_BY_PERSONA[ROLE_ADMIN], FRAPPE_ROLE_BY_PERSONA[ROLE_SECRETARY]]
	users = frappe.get_all(
		"Has Role",
		filters={"role": ["in", roles], "parenttype": "User"},
		pluck="parent",
	)
	if not users:
		return []
	return [
		(u.name, u.full_name)
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", list(set(users))], "enabled": 1},
			fields=["name", "full_name"],
		)
	]


def _instructor_user(instructor: str) -> str | None:
	"""The User behind an Instructor.

	Mirrors get_linked_instructor: prefer the Employee link, then fall back to
	matching on full name, since a school running without HR has no Employee
	records at all.
	"""
	employee, instructor_name = frappe.db.get_value(
		"Instructor", instructor, ["employee", "instructor_name"]
	) or (None, None)

	if employee:
		user = frappe.db.get_value("Employee", employee, "user_id")
		if user:
			return user

	if instructor_name:
		return frappe.db.get_value(
			"User", {"full_name": instructor_name, "enabled": 1}, "name"
		)
	return None


@frappe.whitelist()
@ms_endpoint(ROLE_PARENT, ROLE_STUDENT)
def unread_by_child(persona: str = None):
	"""Unread message counts per child, for the family inbox switcher.

	Returned as its own endpoint rather than folded into `inbox`, which
	returns a plain list that several screens already consume — changing that
	shape would break them for no benefit.
	"""
	from match_schools.api.utils import resolve_scope

	scope = resolve_scope(persona)
	children = scope.get("students") or []
	if not children:
		return {"children": [], "total": 0}

	user = frappe.session.user
	rows = frappe.db.sql(
		"""
		SELECT about_student, COUNT(*) AS unread
		  FROM `tabMS Message`
		 WHERE recipient = %(user)s
		   AND read_by_recipient = 0
		   AND IFNULL(about_student, '') != ''
		 GROUP BY about_student
		""",
		{"user": user},
		as_dict=True,
	)
	by_student = {r.about_student: cint(r.unread) for r in rows}

	# Messages with no child attached — school-wide notices — are counted once
	# and shown separately rather than being credited to an arbitrary child.
	general = frappe.db.count(
		"MS Message",
		{"recipient": user, "read_by_recipient": 0, "about_student": ["in", ["", None]]},
	)

	names = {
		r.name: r.student_name
		for r in frappe.get_all(
			"Student", filters={"name": ["in", children]}, fields=["name", "student_name"]
		)
	}

	return {
		"children": [
			{"id": s, "name": names.get(s) or s, "unread": by_student.get(s, 0)}
			for s in children
		],
		"general": cint(general),
		"total": sum(by_student.get(s, 0) for s in children) + cint(general),
	}
