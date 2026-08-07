# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Who may message whom, and how recipients are chosen.

The rules the school asked for:

  * A student may write to the teachers who actually teach them. Reaching
    anyone else is off by default and opens up only if the school turns
    ``student_open_messaging`` on in settings.
  * A teacher may write to any student.
  * The back office may write to anyone, and may read any conversation
    involving a student — that oversight is the point of the setting above.
  * Recipients can be picked individually, by section, or by course, so a
    teacher does not have to tick forty boxes to reach a class.
"""

import frappe
import frappe.defaults
from frappe import _
from frappe.utils import cint, now_datetime

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	ms_endpoint,
	resolve_scope,
)

# School-wide switch: may a student message outside their own teachers?
OPEN_MESSAGING_KEY = "ms_student_open_messaging"

# A student or parent may only ever address a handful of people; staff can
# legitimately need to reach a whole cohort, so the ceiling differs by role.
MAX_RECIPIENTS = 200
MAX_RECIPIENTS_STAFF = 1000


def student_open_messaging() -> bool:
	value = frappe.db.get_default(OPEN_MESSAGING_KEY)
	return cint(value or 0) == 1


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def set_open_messaging(enabled: int, persona: str = None):
	"""Let the school decide whether students may write to anyone."""
	frappe.db.set_default(OPEN_MESSAGING_KEY, cint(enabled), parent="__default")
	frappe.db.commit()
	return {
		"success": True,
		"data": {"student_open_messaging": bool(cint(enabled))},
		"message_en": "Messaging policy updated.",
		"message_ar": "تم تحديث سياسة المراسلة.",
	}


# --- Audience -------------------------------------------------------------


def _instructor_user(instructor: str) -> str | None:
	from match_schools.api.communication import _instructor_user as resolve

	return resolve(instructor)


def _groups_of_student(student: str) -> list[str]:
	return [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	]


def teachers_of_student(student: str) -> set[str]:
	"""Users who teach this student, via the group roster or the timetable."""
	groups = _groups_of_student(student)
	if not groups:
		return set()

	instructors = {
		r.instructor
		for r in frappe.get_all(
			"Student Group Instructor",
			filters={"parent": ["in", groups], "parenttype": "Student Group"},
			fields=["instructor"],
		)
	}
	instructors |= {
		r.instructor
		for r in frappe.get_all(
			"Course Schedule",
			filters={"student_group": ["in", groups], "instructor": ["is", "set"]},
			fields=["instructor"],
			limit=500,
		)
		if r.instructor
	}

	return {u for u in (_instructor_user(i) for i in instructors) if u}


def allowed_recipients(persona: str, scope: dict) -> set[str] | None:
	"""The set a persona may write to, or None when unrestricted."""
	if persona in BACK_OFFICE or persona == ROLE_TEACHER:
		# Staff may reach anyone; a teacher writing to any student is explicitly
		# allowed by the school.
		return None

	from match_schools.api.communication import _back_office_users

	office = {u for u, _name in _back_office_users()}

	if persona == ROLE_STUDENT:
		if student_open_messaging():
			return None
		student = scope.get("student")
		return (teachers_of_student(student) | office) if student else office

	if persona == ROLE_PARENT:
		if student_open_messaging():
			return None
		allowed = set(office)
		for child in scope.get("students") or []:
			allowed |= teachers_of_student(child)
		return allowed

	return office


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def audience(search: str = None, persona: str = None):
	"""Everything the caller may address: people, sections and courses."""
	scope = resolve_scope(persona)
	allowed = allowed_recipients(persona, scope)

	from match_schools.api.communication import contacts as contact_list

	envelope = contact_list(persona=persona)
	people = envelope.get("data") if isinstance(envelope, dict) else envelope
	people = people or []

	if allowed is not None:
		people = [p for p in people if p["user"] in allowed]

	if search:
		needle = search.strip().lower()
		people = [
			p
			for p in people
			if needle in (p.get("name") or "").lower() or needle in (p.get("user") or "").lower()
		]

	return {
		"people": people[:MAX_RECIPIENTS],
		"groups": _addressable_groups(persona, scope),
		"courses": _addressable_courses(persona, scope),
		"policy": {
			"student_open_messaging": student_open_messaging(),
			"restricted": allowed is not None,
		},
	}


def _addressable_groups(persona: str, scope: dict) -> list[dict]:
	"""Sections the caller may address as a whole."""
	filters: dict = {}
	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return []
		groups = {
			r.parent
			for r in frappe.get_all(
				"Student Group Instructor",
				filters={"instructor": instructor, "parenttype": "Student Group"},
				fields=["parent"],
			)
		}
		groups |= {
			r.student_group
			for r in frappe.get_all(
				"Course Schedule", filters={"instructor": instructor}, fields=["student_group"]
			)
			if r.student_group
		}
		if not groups:
			return []
		filters["name"] = ["in", list(groups)]
	elif persona in (ROLE_STUDENT, ROLE_PARENT):
		# A student may address their own section — classmates and teachers.
		own = set()
		for s in scope.get("students") or []:
			own |= set(_groups_of_student(s))
		if not own:
			return []
		filters["name"] = ["in", list(own)]

	rows = frappe.get_all(
		"Student Group",
		filters=filters,
		fields=["name", "student_group_name", "program", "batch"],
		order_by="student_group_name",
		limit=200,
	)
	return [
		{
			"id": r.name,
			"name": r.student_group_name or r.name,
			"program": r.program,
			"batch": r.batch,
			"members": frappe.db.count(
				"Student Group Student",
				{"parent": r.name, "parenttype": "Student Group", "active": 1},
			),
		}
		for r in rows
	]


def _addressable_courses(persona: str, scope: dict) -> list[dict]:
	"""Courses the caller may address — everyone enrolled in them."""
	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return []
		courses = {
			r.course
			for r in frappe.get_all(
				"Course Schedule",
				filters={"instructor": instructor},
				fields=["course"],
				limit=500,
			)
			if r.course
		}
		# A teacher assigned to a section but with no timetable rows of their
		# own still teaches whatever that section is taught.
		if not courses:
			groups = [g["id"] for g in _addressable_groups(persona, scope)]
			if groups:
				courses = {
					r.course
					for r in frappe.get_all(
						"Course Schedule",
						filters={"student_group": ["in", groups]},
						fields=["course"],
						limit=500,
					)
					if r.course
				}
	elif persona in (ROLE_STUDENT, ROLE_PARENT):
		# Only the courses the student is actually enrolled in.
		courses = set()
		for enrollment in frappe.get_all(
			"Program Enrollment",
			filters={"student": ["in", scope.get("students") or [""]], "docstatus": ["<", 2]},
			pluck="name",
		):
			courses |= {
				r.course
				for r in frappe.get_all(
					"Program Enrollment Course",
					filters={"parent": enrollment, "parenttype": "Program Enrollment"},
					fields=["course"],
				)
				if r.course
			}
	else:
		courses = set(frappe.get_all("Course", pluck="name", limit=300))

	if not courses:
		return []

	rows = frappe.get_all(
		"Course",
		filters={"name": ["in", list(courses)]},
		fields=["name", "course_name"],
		order_by="course_name",
		limit=300,
	)
	return [{"id": r.name, "name": r.course_name or r.name} for r in rows]


# --- Expanding a target into users ----------------------------------------


def _users_of_group(group: str) -> set[str]:
	"""Every student account in a section, plus the teachers who teach it."""
	users = set()
	students = frappe.get_all(
		"Student Group Student",
		filters={"parent": group, "parenttype": "Student Group", "active": 1},
		pluck="student",
	)
	if students:
		users |= {
			u
			for u in frappe.get_all(
				"Student", filters={"name": ["in", students], "enabled": 1}, pluck="user"
			)
			if u
		}
	return users


def _users_of_course(course: str) -> set[str]:
	"""Every student enrolled in a course."""
	enrollments = frappe.get_all(
		"Program Enrollment Course",
		filters={"course": course, "parenttype": "Program Enrollment"},
		fields=["parent"],
		limit=2000,
	)
	if not enrollments:
		return set()

	students = frappe.get_all(
		"Program Enrollment",
		filters={"name": ["in", [e.parent for e in enrollments]], "docstatus": ["<", 2]},
		pluck="student",
	)
	if not students:
		return set()

	return {
		u
		for u in frappe.get_all(
			"Student", filters={"name": ["in", list(set(students))], "enabled": 1}, pluck="user"
		)
		if u
	}


def resolve_targets(
	recipients: list[str] | None,
	groups: list[str] | None,
	courses: list[str] | None,
) -> set[str]:
	"""Flatten however the sender chose their audience into a set of users."""
	users: set[str] = set(recipients or [])
	for g in groups or []:
		users |= _users_of_group(g)
	for c in courses or []:
		users |= _users_of_course(c)
	users.discard(frappe.session.user)
	return users


# --- Sending --------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def send(
	body: str,
	recipients: str | list = None,
	groups: str | list = None,
	courses: str | list = None,
	subject: str = None,
	thread: str = None,
	files: str | list = None,
	about_student: str = None,
	persona: str = None,
):
	"""Send one message to everyone named, directly or by section/course."""
	from match_schools.api.assignments import _normalise_files
	from match_schools.api.communication import _as_html

	recipients = frappe.parse_json(recipients) if isinstance(recipients, str) else recipients
	groups = frappe.parse_json(groups) if isinstance(groups, str) else groups
	courses = frappe.parse_json(courses) if isinstance(courses, str) else courses

	attachments = _normalise_files(files)
	if not (body or "").strip() and not attachments:
		return fail(
			message_en="Write a message or attach a file.",
			message_ar="اكتب رسالة أو أرفق ملفاً.",
		)

	targets = resolve_targets(recipients, groups, courses)
	if not targets:
		return fail(
			message_en="Choose at least one recipient.",
			message_ar="اختر مستلماً واحداً على الأقل.",
		)
	ceiling = (
		MAX_RECIPIENTS_STAFF
		if persona in BACK_OFFICE or persona == ROLE_TEACHER
		else MAX_RECIPIENTS
	)
	if len(targets) > ceiling:
		return fail(
			message_en=f"Too many recipients (max {ceiling}).",
			message_ar=f"عدد المستلمين كبير جداً (الحد {ceiling}).",
		)

	scope = resolve_scope(persona)
	allowed = allowed_recipients(persona, scope)
	if allowed is not None:
		blocked = targets - allowed
		if blocked:
			return fail(
				message_en="You are not allowed to message some of these people.",
				message_ar="لا يمكنك مراسلة بعض هؤلاء الأشخاص.",
			)

	html = _as_html(body)
	sent = []
	for recipient in sorted(targets):
		doc = frappe.get_doc(
			{
				"doctype": "MS Message",
				"sender": frappe.session.user,
				"recipient": recipient,
				"subject": subject,
				"body": html,
				# A broadcast starts a separate conversation per recipient, so
				# replies stay private between the two of them.
				"thread": thread,
				"about_student": about_student,
				"sent_on": now_datetime(),
			}
		)
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
		doc.insert(ignore_permissions=True)
		sent.append(doc.name)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"sent": len(sent), "messages": sent, "recipients": sorted(targets)},
		"message_en": f"Sent to {len(sent)} recipient(s).",
		"message_ar": f"تم الإرسال إلى {len(sent)} مستلم.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def conversation(thread: str, persona: str = None):
	"""One conversation with its attachments.

	A back-office user may open any conversation that involves a student, which
	is the oversight the open-messaging setting is predicated on.
	"""
	rows = frappe.get_all(
		"MS Message",
		filters={"thread": thread},
		fields=["name", "sender", "recipient", "subject", "body", "sent_on", "read_by_recipient"],
		order_by="sent_on",
	)
	if not rows:
		# A conversation that never got a reply is stored under its own name.
		rows = frappe.get_all(
			"MS Message",
			filters={"name": thread},
			fields=[
				"name", "sender", "recipient", "subject", "body", "sent_on", "read_by_recipient",
			],
		)
	if not rows:
		return fail(message_en="Conversation not found.", message_ar="لم يتم العثور على المحادثة.")

	user = frappe.session.user
	participants = {r.sender for r in rows} | {r.recipient for r in rows}
	if user not in participants and persona not in BACK_OFFICE:
		frappe.throw(_("You are not part of this conversation."), frappe.PermissionError)

	files_by_message: dict[str, list] = {}
	for f in frappe.get_all(
		"MS Attachment",
		filters={"parent": ["in", [r.name for r in rows]], "parenttype": "MS Message"},
		fields=["parent", "file_url", "file_name", "file_size"],
		order_by="idx",
	):
		files_by_message.setdefault(f.parent, []).append(
			{"file_url": f.file_url, "file_name": f.file_name, "file_size": f.file_size}
		)

	# Opening a conversation marks the caller's own incoming messages as read.
	unread = [r.name for r in rows if r.recipient == user and not r.read_by_recipient]
	if unread:
		for name in unread:
			frappe.db.set_value("MS Message", name, "read_by_recipient", 1, update_modified=False)
		frappe.db.commit()

	names = {u: frappe.db.get_value("User", u, "full_name") or u for u in participants}

	return {
		"thread": thread,
		"subject": rows[0].subject,
		"participants": [{"user": u, "name": names[u]} for u in sorted(participants)],
		"observing": user not in participants,
		"messages": [
			{
				"id": r.name,
				"sender": r.sender,
				"sender_name": names.get(r.sender, r.sender),
				"recipient": r.recipient,
				"body": r.body,
				"sent_on": str(r.sent_on or ""),
				"outgoing": r.sender == user,
				"read": bool(r.read_by_recipient),
				"files": files_by_message.get(r.name, []),
			}
			for r in rows
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def student_conversations(student: str = None, limit: int = 50, persona: str = None):
	"""Oversight: every conversation a student is part of."""
	limit = min(max(cint(limit) or 50, 1), 200)

	if student:
		user = frappe.db.get_value("Student", student, "user")
		if not user:
			return {"rows": [], "student": student}
		rows = frappe.get_all(
			"MS Message",
			or_filters=[["sender", "=", user], ["recipient", "=", user]],
			fields=["name", "thread", "subject", "sender", "recipient", "body", "sent_on"],
			order_by="sent_on desc",
			limit=limit,
		)
	else:
		# Every student account, so the office can scan recent traffic.
		student_users = [
			u for u in frappe.get_all("Student", filters={"enabled": 1}, pluck="user") if u
		]
		if not student_users:
			return {"rows": [], "student": None}
		rows = frappe.get_all(
			"MS Message",
			or_filters=[
				["sender", "in", student_users],
				["recipient", "in", student_users],
			],
			fields=["name", "thread", "subject", "sender", "recipient", "body", "sent_on"],
			order_by="sent_on desc",
			limit=limit,
		)

	names: dict[str, str] = {}

	def name_of(user: str) -> str:
		if user not in names:
			names[user] = frappe.db.get_value("User", user, "full_name") or user
		return names[user]

	return {
		"student": student,
		"rows": [
			{
				"id": r.name,
				"thread": r.thread or r.name,
				"subject": r.subject,
				"preview": frappe.utils.strip_html(r.body or "")[:120],
				"from": name_of(r.sender),
				"from_user": r.sender,
				"to": name_of(r.recipient),
				"to_user": r.recipient,
				"time": str(r.sent_on or ""),
			}
			for r in rows
		],
	}
