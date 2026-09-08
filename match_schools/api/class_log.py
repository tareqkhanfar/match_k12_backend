"""دفتر الحصص — a record of what actually happened in each lesson.

The lesson plan says what a teacher meant to do; this says what the class did.
They are separate records on purpose: a plan written last week and then edited
into a record of the lesson leaves no trace of either, and a head asking "was
this covered" gets an answer that was written after the fact.

Written for families as much as for the school. A pupil who was absent, or a
parent asking what was done today, currently depends on the pupil remembering.
So a log is visible to the class by default, and the teacher can hold one back
rather than the other way round.

Reachable two ways, because the timetable is where a teacher already is when
the lesson ends: from the slot itself, and from a screen of its own for
catching up on a week at once.
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate, now, nowdate

from match_schools.api.utils import (
	apply_period,
	fail,
	hhmm,
	instructor_groups,
	ms_endpoint,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
ALL_ROLES = (*STAFF, ROLE_STUDENT, ROLE_PARENT)

MAX_FILES = 10
MAX_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {
	"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf",
	"png", "jpg", "jpeg", "gif", "webp", "mp3", "mp4", "webm", "zip",
}

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _my_groups(persona: str) -> list[str] | None:
	"""Classes the caller may read logs for. `None` means all of them."""
	if persona in BACK_OFFICE:
		return None

	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		# مصدر واحد لكل الشاشات، ومقيَّد بالفصل المختار.
		return sorted(
			set(instructor_groups(scope.get("instructor")))
			| set(scope.get("student_groups") or [])
		)

	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", scope.get("students") or [""]], "active": 1},
				fields=["parent"],
			)
		}
	)


def _assert_may_write(persona: str, student_group: str) -> None:
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		frappe.throw(_("You cannot write in the class book."), frappe.PermissionError)
	groups = _my_groups(persona)
	if groups is not None and student_group not in groups:
		frappe.throw(_("This class is not yours."), frappe.PermissionError)


def _row(doc, persona: str) -> dict:
	return {
		"id": doc.name,
		"date": str(doc.log_date or ""),
		"student_group": doc.student_group,
		"course": doc.course,
		"instructor": doc.instructor,
		"topic": doc.topic,
		"what_was_done": doc.what_was_done,
		"homework": doc.homework,
		"notes": doc.notes if persona in STAFF else None,
		"from_time": hhmm(doc.from_time) if doc.from_time else "",
		"to_time": hhmm(doc.to_time) if doc.to_time else "",
		"period_order": cint(doc.period_order),
		"is_published": bool(cint(doc.is_published)),
		"created_by": doc.created_by_user,
		"created_on": str(doc.created_on or ""),
		"attachments": [
			{"file_url": f.file_url, "file_name": f.file_name, "file_size": cint(f.file_size)}
			for f in (doc.attachments or [])
		],
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def list_logs(
	student_group: str = None,
	course: str = None,
	from_date: str = None,
	to_date: str = None,
	limit: int = 60,
	persona: str = None,
):
	"""Lesson records the caller may read, newest first."""
	filters: dict = {}
	groups = _my_groups(persona)
	if groups is not None:
		if not groups:
			return {"logs": [], "total": 0}
		filters["student_group"] = ["in", groups]
	if student_group:
		if groups is not None and student_group not in groups:
			frappe.throw(_("This class is not yours."), frappe.PermissionError)
		filters["student_group"] = student_group
	if course:
		filters["course"] = course
	if from_date and to_date:
		filters["log_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["log_date"] = [">=", from_date]
	elif to_date:
		filters["log_date"] = ["<=", to_date]

	# An unpublished log is the teacher's own note until they release it.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		filters["is_published"] = 1

	apply_period(filters, "MS Class Log")

	names = frappe.get_all(
		"MS Class Log",
		filters=filters,
		pluck="name",
		order_by="log_date desc, period_order asc, creation desc",
		limit_page_length=min(max(cint(limit) or 60, 1), 200),
	)
	logs = [_row(frappe.get_doc("MS Class Log", n), persona) for n in names]

	group_names = {
		g.name: g.student_group_name
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", list({r["student_group"] for r in logs}) or [""]]},
			fields=["name", "student_group_name"],
		)
	}
	for row in logs:
		row["group_name"] = group_names.get(row["student_group"]) or row["student_group"]

	return {"logs": logs, "total": len(logs)}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def get_log(log: str = None, persona: str = None):
	"""One lesson record."""
	if not log:
		return fail(message_en="A record is required.", message_ar="يجب تحديد السجل.")

	doc = frappe.get_doc("MS Class Log", log)
	groups = _my_groups(persona)
	if groups is not None and doc.student_group not in groups:
		frappe.throw(_("This class is not yours."), frappe.PermissionError)
	if persona in (ROLE_STUDENT, ROLE_PARENT) and not cint(doc.is_published):
		frappe.throw(_("This record is not published."), frappe.PermissionError)

	return _row(doc, persona)


@frappe.whitelist()
@ms_endpoint(*STAFF)
def day_slots(student_group: str = None, date: str = None, persona: str = None):
	"""The lessons a class has that day, and which already have a record.

	This is the entry point from the timetable: a teacher finishing period
	three should see period three, not an empty form asking which lesson they
	mean.
	"""
	date = date or nowdate()
	weekday = DAYS[(getdate(date).weekday() + 1) % 7]

	filters = {"day": weekday, "active": 1}
	if student_group:
		filters["student_group"] = student_group
	else:
		groups = _my_groups(persona)
		if groups is not None:
			if not groups:
				return {"date": date, "day": weekday, "slots": [], "logged": 0}
			filters["student_group"] = ["in", groups]

	if persona == ROLE_TEACHER:
		instructor = resolve_scope(persona).get("instructor")
		if instructor:
			filters["instructor"] = instructor

	slots = frappe.get_all(
		"MS Timetable Slot",
		filters=filters,
		fields=[
			"name", "student_group", "course", "instructor", "from_time",
			"to_time", "period_order", "room",
		],
		order_by="period_order",
		limit_page_length=0,
	)
	# The same shape whether or not there are lessons: a caller reading
	# `logged` should not get a number on some days and nothing on others.
	if not slots:
		return {"date": date, "day": weekday, "slots": [], "logged": 0}

	logged = {
		r.timetable_slot: r.name
		for r in frappe.get_all(
			"MS Class Log",
			filters={"log_date": date, "timetable_slot": ["in", [s.name for s in slots]]},
			fields=["name", "timetable_slot"],
		)
		if r.timetable_slot
	}
	# A log written without picking a slot still counts as "this lesson is
	# done" when the class, subject and date line up.
	loose = frappe.get_all(
		"MS Class Log",
		filters={
			"log_date": date,
			"student_group": ["in", list({s.student_group for s in slots})],
		},
		fields=["name", "student_group", "course", "timetable_slot"],
	)

	group_names = {
		g.name: g.student_group_name
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", list({s.student_group for s in slots})]},
			fields=["name", "student_group_name"],
		)
	}

	# A log written without picking a slot is matched to one lesson only, and
	# then taken out of the pool. Leaving it in marked every period of that
	# subject as documented — so a teacher who wrote up one of their three
	# maths lessons was told all three were done.
	unclaimed = [x for x in loose if not x.timetable_slot]

	out = []
	for s in slots:
		log = logged.get(s.name)
		if not log:
			match = next(
				(
					x
					for x in unclaimed
					if x.student_group == s.student_group and x.course == s.course
				),
				None,
			)
			if match:
				unclaimed.remove(match)
			log = match.name if match else None
		out.append(
			{
				"slot": s.name,
				"student_group": s.student_group,
				"group_name": group_names.get(s.student_group) or s.student_group,
				"course": s.course,
				"from_time": hhmm(s.from_time) if s.from_time else "",
				"to_time": hhmm(s.to_time) if s.to_time else "",
				"period_order": cint(s.period_order),
				"room": s.room,
				"log": log,
				"logged": bool(log),
			}
		)

	return {
		"date": date,
		"day": weekday,
		"slots": out,
		"logged": sum(1 for s in out if s["logged"]),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def save_log(payload: str | dict = None, persona: str = None):
	"""Write or update one lesson record."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	student_group = data.get("student_group")
	log_date = data.get("date") or data.get("log_date")

	if not (student_group and log_date):
		return fail(
			message_en="A class and a date are required.",
			message_ar="يجب تحديد الشعبة والتاريخ.",
		)
	if getdate(log_date) > getdate(nowdate()):
		# A record of a lesson that has not happened is a plan, and there is a
		# screen for those.
		return fail(
			message_en="A lesson record cannot be dated in the future.",
			message_ar="لا يمكن توثيق حصة لم تحدث بعد — استخدم تحضير الدرس.",
		)
	_assert_may_write(persona, student_group)

	name = data.get("log")
	if name:
		doc = frappe.get_doc("MS Class Log", name)
		_assert_may_write(persona, doc.student_group)
		if persona == ROLE_TEACHER and doc.created_by_user != frappe.session.user:
			frappe.throw(_("This record was written by someone else."), frappe.PermissionError)
	else:
		doc = frappe.new_doc("MS Class Log")
		doc.created_by_user = frappe.session.user
		doc.created_on = now()

	slot = data.get("timetable_slot")
	if slot:
		row = frappe.db.get_value(
			"MS Timetable Slot",
			slot,
			["from_time", "to_time", "period_order", "course", "instructor"],
			as_dict=True,
		)
		if row:
			doc.timetable_slot = slot
			doc.from_time = row.from_time
			doc.to_time = row.to_time
			doc.period_order = row.period_order
			doc.course = doc.course or row.course
			doc.instructor = row.instructor

	doc.log_date = log_date
	doc.student_group = student_group
	doc.course = data.get("course") or doc.course
	doc.topic = (data.get("topic") or "")[:180] or None
	doc.what_was_done = data.get("what_was_done")
	doc.homework = data.get("homework")
	doc.notes = data.get("notes")

	was_published = cint(doc.is_published)
	doc.is_published = 1 if cint(data.get("is_published", 1)) else 0
	if doc.is_published and not was_published:
		doc.published_on = now()

	if persona == ROLE_TEACHER and not doc.instructor:
		doc.instructor = resolve_scope(persona).get("instructor")

	files = data.get("attachments")
	if files is not None:
		if len(files) > MAX_FILES:
			return fail(
				message_en=f"At most {MAX_FILES} files.",
				message_ar=f"الحد الأقصى {MAX_FILES} ملفات.",
			)
		doc.set("attachments", [])
		for f in files:
			url = (f or {}).get("file_url")
			if url:
				doc.append(
					"attachments",
					{
						"file_url": url,
						"file_name": f.get("file_name"),
						"file_size": cint(f.get("file_size")),
						"uploaded_on": now(),
					},
				)

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Lesson record saved.",
		"message_ar": "تم حفظ سجل الحصة.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def delete_log(log: str = None, persona: str = None):
	"""Remove a lesson record."""
	if not log:
		return fail(message_en="A record is required.", message_ar="يجب تحديد السجل.")

	doc = frappe.get_doc("MS Class Log", log)
	_assert_may_write(persona, doc.student_group)
	if persona == ROLE_TEACHER and doc.created_by_user != frappe.session.user:
		frappe.throw(_("This record was written by someone else."), frappe.PermissionError)

	frappe.delete_doc("MS Class Log", log, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": log},
		"message_en": "Record deleted.",
		"message_ar": "تم حذف السجل.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def upload_attachment(persona: str = None):
	"""Store one file against a lesson record."""
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(message_en="No file received.", message_ar="لم يتم استلام أي ملف.")

	filename = uploaded.filename or "file"
	extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	if extension not in ALLOWED_EXTENSIONS:
		return fail(
			message_en=f"File type .{extension} is not allowed.",
			message_ar=f"نوع الملف .{extension} غير مسموح.",
		)

	content = uploaded.stream.read()
	if len(content) > MAX_FILE_BYTES:
		return fail(
			message_en="File is larger than 25 MB.",
			message_ar="حجم الملف يتجاوز ٢٥ ميجابايت.",
		)

	# Not private: a lesson record is meant to be read by the class it is for.
	file_doc = frappe.get_doc(
		{"doctype": "File", "file_name": filename, "content": content, "is_private": 1}
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
