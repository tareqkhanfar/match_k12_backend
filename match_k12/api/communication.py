# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Announcements and direct messages."""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, today

from match_k12.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	k12_endpoint,
	resolve_scope,
	ROLE_SECRETARY,
)

TYPE_AR = {"Announcement": "إعلان", "Event": "حدث", "Alert": "تنبيه"}

# Which announcement audiences each persona should receive.
AUDIENCE_FOR_PERSONA = {
	ROLE_ADMIN: ["All", "Students", "Teachers", "Parents", "Program", "Student Group"],
	ROLE_TEACHER: ["All", "Teachers"],
	ROLE_STUDENT: ["All", "Students"],
	ROLE_PARENT: ["All", "Parents"],
}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_announcements(limit: int = 25, persona: str = None):
	"""Announcements targeted at the caller."""
	limit = min(max(cint(limit) or 25, 1), 100)
	scope = resolve_scope(persona)

	audiences = AUDIENCE_FOR_PERSONA.get(persona, ["All"])
	filters = {"published": 1, "audience": ["in", audiences]}

	rows = frappe.get_all(
		"K12 Announcement",
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
				"K12 Announcement",
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
		doc = frappe.get_doc("K12 Announcement", announcement_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Announcement updated.", "تم تحديث الإعلان."
	else:
		if not fields.get("title"):
			return fail(message_en="Title is required.", message_ar="العنوان مطلوب.")
		fields["doctype"] = "K12 Announcement"
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_announcement(announcement: str, persona: str = None):
	frappe.delete_doc("K12 Announcement", announcement)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": announcement},
		"message_en": "Announcement deleted.",
		"message_ar": "تم حذف الإعلان.",
	}


# --- Messages --------------------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def inbox(limit: int = 50, persona: str = None):
	"""Latest message per conversation involving the current user."""
	user = frappe.session.user
	limit = min(max(cint(limit) or 50, 1), 200)

	rows = frappe.db.sql(
		"""
		SELECT m.name, m.thread, m.subject, m.body, m.sender, m.recipient,
			m.sent_on, m.read_by_recipient, m.about_student
		FROM `tabK12 Message` m
		INNER JOIN (
			SELECT thread, MAX(sent_on) AS latest
			FROM `tabK12 Message`
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

	out = []
	for r in rows:
		other = r.recipient if r.sender == user else r.sender
		unread = frappe.db.count(
			"K12 Message",
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
	from match_k12.api.utils import get_persona

	labels = {
		ROLE_ADMIN: "إدارة",
		ROLE_TEACHER: "معلم",
		ROLE_STUDENT: "طالب",
		ROLE_PARENT: "ولي أمر",
	}
	return labels.get(get_persona(user), "")


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def thread(thread: str, persona: str = None):
	"""Full conversation; marks the caller's incoming messages as read."""
	user = frappe.session.user
	rows = frappe.get_all(
		"K12 Message",
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
		frappe.db.set_value("K12 Message", name, "read_by_recipient", 1, update_modified=False)
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


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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
	if not frappe.db.exists("User", recipient):
		return fail(message_en="Recipient not found.", message_ar="لم يتم العثور على المستلم.")

	doc = frappe.get_doc(
		{
			"doctype": "K12 Message",
			"sender": frappe.session.user,
			"recipient": recipient,
			"subject": subject,
			"body": body,
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def contacts(persona: str = None):
	"""Users the caller is allowed to message."""
	scope = resolve_scope(persona)
	out = []

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		# Reach the teachers of the student's groups, plus school admins.
		groups = _groups_for_students(scope.get("students") or [])
		instructors = (
			frappe.get_all(
				"Student Group Instructor",
				filters={"parent": ["in", groups], "parenttype": "Student Group"},
				fields=["instructor", "instructor_name"],
			)
			if groups
			else []
		)
		seen = set()
		for i in instructors:
			if i.instructor in seen:
				continue
			seen.add(i.instructor)
			user = _instructor_user(i.instructor)
			if user:
				out.append({"user": user, "name": i.instructor_name, "role": "معلم"})
	else:
		# Teachers and admins can message guardians of their students.
		if persona == ROLE_TEACHER:
			from match_k12.api.students import _students_of_instructor

			students = _students_of_instructor(scope.get("instructor"))
		else:
			students = frappe.get_all("Student", filters={"enabled": 1}, pluck="name", limit=500)

		guardians = (
			frappe.get_all(
				"Student Guardian",
				filters={"parent": ["in", students], "parenttype": "Student"},
				fields=["guardian", "guardian_name"],
			)
			if students
			else []
		)
		seen = set()
		for g in guardians:
			if g.guardian in seen:
				continue
			seen.add(g.guardian)
			user = frappe.db.get_value("Guardian", g.guardian, "user")
			if user:
				out.append({"user": user, "name": g.guardian_name, "role": "ولي أمر"})

	return out


def _instructor_user(instructor: str) -> str | None:
	employee = frappe.db.get_value("Instructor", instructor, "employee")
	if employee:
		return frappe.db.get_value("Employee", employee, "user_id")
	return None
