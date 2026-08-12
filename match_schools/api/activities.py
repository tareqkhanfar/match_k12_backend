# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Clubs, trips and extracurricular activities.

An activity is open to the whole school, to a program, or to a single class.
Students register (or are registered by staff); when the activity is full the
registration is waitlisted rather than refused, so the office can promote from
the list if someone withdraws.

A trip usually needs guardian consent. Where an activity requires it, the
registration is not confirmed until the guardian grants it — and only the
guardian of that child may do so.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from match_schools.api.utils import (
	hhmm,
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	build_order_by,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	paginate,
	parse_json_arg,
	resolve_scope,
)

TYPE_AR = {
	"Club": "نادي",
	"Trip": "رحلة",
	"Competition": "مسابقة",
	"Workshop": "ورشة عمل",
	"Community Service": "خدمة مجتمعية",
	"Sports": "نشاط رياضي",
	"Other": "أخرى",
}
STATUS_AR = {
	"Planned": "قيد التخطيط",
	"Open": "التسجيل مفتوح",
	"Closed": "التسجيل مغلق",
	"Completed": "منتهية",
	"Cancelled": "ملغاة",
}
ENROLMENT_AR = {
	"Registered": "مُسجّل",
	"Waitlisted": "قائمة الانتظار",
	"Confirmed": "مؤكد",
	"Withdrawn": "منسحب",
}
CONSENT_AR = {
	"Not Required": "غير مطلوبة",
	"Pending": "بانتظار الموافقة",
	"Granted": "تمت الموافقة",
	"Declined": "مرفوضة",
}


def _eligible_activities(persona: str, scope: dict) -> list[str] | None:
	"""Activity names this persona may see. None means all."""
	if persona in BACK_OFFICE or persona == ROLE_TEACHER:
		return None

	students = scope.get("students") or []
	if not students:
		return []

	groups = {
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students], "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	}
	programs = {
		r.program
		for r in frappe.get_all(
			"Program Enrollment",
			filters={"student": ["in", students], "docstatus": ["<", 2]},
			fields=["program"],
		)
		if r.program
	}

	names = set(frappe.get_all("MS Activity", filters={"target_audience": "All"}, pluck="name"))
	if programs:
		names |= set(
			frappe.get_all(
				"MS Activity",
				filters={"target_audience": "Program", "program": ["in", list(programs)]},
				pluck="name",
			)
		)
	if groups:
		names |= set(
			frappe.get_all(
				"MS Activity",
				filters={"target_audience": "Student Group", "student_group": ["in", list(groups)]},
				pluck="name",
			)
		)
	return sorted(names)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_activities(
	activity_type: str = None,
	status: str = None,
	search: str = None,
	page: int = 1,
	page_size: int = 20,
	persona: str = None,
):
	"""Activities visible to the caller, with registration counts."""
	scope = resolve_scope(persona)
	eligible = _eligible_activities(persona, scope)
	if eligible is not None and not eligible:
		return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}

	filters = {}
	if eligible is not None:
		filters["name"] = ["in", eligible]
	if activity_type:
		filters["activity_type"] = activity_type
	if status:
		filters["status"] = status
	if search:
		filters["title"] = ["like", f"%{search}%"]

	# Students and parents never see something still being planned.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		filters["status"] = ["in", ["Open", "Closed", "Completed"]]

	total = frappe.db.count("MS Activity", filters)
	page, page_size, offset = paginate(page, page_size)
	rows = frappe.get_all(
		"MS Activity",
		filters=filters,
		fields=[
			"name", "title", "activity_type", "status", "start_date", "end_date",
			"from_time", "to_time", "location", "capacity", "fee", "supervisor",
			"target_audience", "program", "student_group", "requires_consent",
			"registration_deadline", "description",
		],
		order_by="start_date desc",
		start=offset,
		page_length=page_size,
	)

	# Counts for every activity in one query, rather than one per row.
	counts: dict[str, dict] = {}
	if rows:
		for e in frappe.get_all(
			"MS Activity Enrolment",
			filters={"activity": ["in", [r.name for r in rows]]},
			fields=["activity", "status"],
		):
			bucket = counts.setdefault(e.activity, {"registered": 0, "waitlisted": 0})
			if e.status == "Waitlisted":
				bucket["waitlisted"] += 1
			elif e.status != "Withdrawn":
				bucket["registered"] += 1

	# Which of these the viewer's own children are already on.
	mine: dict[str, list] = {}
	if persona in (ROLE_STUDENT, ROLE_PARENT) and rows:
		for e in frappe.get_all(
			"MS Activity Enrolment",
			filters={
				"activity": ["in", [r.name for r in rows]],
				"student": ["in", scope.get("students") or [""]],
			},
			fields=["name", "activity", "student", "student_name", "status", "consent_status"],
		):
			mine.setdefault(e.activity, []).append(
				{
					"id": e.name,
					"student": e.student,
					"student_name": e.student_name,
					"status": e.status,
					"status_label": ENROLMENT_AR.get(e.status, e.status),
					"consent_status": e.consent_status,
					"consent_label": CONSENT_AR.get(e.consent_status, e.consent_status),
				}
			)

	stamp = today()
	items = []
	for r in rows:
		count = counts.get(r.name, {"registered": 0, "waitlisted": 0})
		capacity = cint(r.capacity)
		open_for_registration = (
			r.status == "Open"
			and (not r.registration_deadline or str(r.registration_deadline) >= stamp)
		)
		items.append(
			{
				"id": r.name,
				"title": r.title,
				"type": r.activity_type,
				"type_label": TYPE_AR.get(r.activity_type, r.activity_type),
				"status": r.status,
				"status_label": STATUS_AR.get(r.status, r.status),
				"start_date": str(r.start_date or ""),
				"end_date": str(r.end_date or ""),
				"from_time": hhmm(r.from_time),
				"to_time": hhmm(r.to_time),
				"location": r.location,
				"capacity": capacity,
				"fee": flt(r.fee),
				"supervisor": r.supervisor,
				"audience": r.target_audience,
				"program": r.program,
				"student_group": r.student_group,
				"requires_consent": bool(r.requires_consent),
				"registration_deadline": str(r.registration_deadline or ""),
				"description": r.description,
				"registered": count["registered"],
				"waitlisted": count["waitlisted"],
				"seats_left": max(capacity - count["registered"], 0) if capacity else None,
				"full": bool(capacity and count["registered"] >= capacity),
				"open": open_for_registration,
				"upcoming": bool(r.start_date and str(r.start_date) >= stamp),
				"my_enrolments": mine.get(r.name, []),
			}
		)

	return {"items": items, "total": total, "page": page, "page_size": page_size}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_activity(payload: str | dict, persona: str = None):
	"""Create or update an activity."""
	data = parse_json_arg(payload) or {}
	if not data.get("title"):
		return fail(message_en="A title is required.", message_ar="عنوان النشاط مطلوب.")
	if not data.get("start_date"):
		return fail(message_en="A start date is required.", message_ar="تاريخ البداية مطلوب.")

	activity_id = data.get("id")
	doc = (
		frappe.get_doc("MS Activity", activity_id)
		if activity_id
		else frappe.new_doc("MS Activity")
	)

	# A teacher may run an activity but not decide the school's audience rules.
	if persona == ROLE_TEACHER:
		scope = resolve_scope(persona)
		data["supervisor"] = data.get("supervisor") or scope.get("instructor")

	for field in (
		"title", "activity_type", "status", "start_date", "end_date", "from_time",
		"to_time", "location", "supervisor", "target_audience", "program",
		"student_group", "registration_deadline", "description", "notes",
	):
		if data.get(field) is not None:
			setattr(doc, field, data[field])

	doc.capacity = cint(data.get("capacity"))
	doc.fee = flt(data.get("fee"))
	doc.requires_consent = cint(data.get("requires_consent"))
	doc.academic_year = data.get("academic_year") or get_default_academic_year()
	doc.academic_term = data.get("academic_term") or get_default_academic_term()

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.title},
		"message_en": "Activity saved.",
		"message_ar": "تم حفظ النشاط.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_activity(activity: str, persona: str = None):
	"""Remove an activity, provided nobody is registered."""
	enrolled = frappe.db.count(
		"MS Activity Enrolment", {"activity": activity, "status": ["!=", "Withdrawn"]}
	)
	if enrolled:
		return fail(
			message_en=f"{enrolled} student(s) are registered; cancel the activity instead.",
			message_ar=f"يوجد {enrolled} طالب مُسجّل — يمكنك إلغاء النشاط بدل حذفه.",
		)
	frappe.delete_doc("MS Activity", activity, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": activity},
		"message_en": "Activity deleted.",
		"message_ar": "تم حذف النشاط.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def register(activity: str, student: str = None, persona: str = None):
	"""Register a student, waitlisting them when the activity is full."""
	scope = resolve_scope(persona)

	if persona == ROLE_STUDENT:
		student = scope.get("student")
	elif persona == ROLE_PARENT:
		if student not in (scope.get("students") or []):
			frappe.throw(_("You can only register your own children."), frappe.PermissionError)
	if not student:
		return fail(message_en="No student given.", message_ar="لم يتم تحديد الطالب.")

	doc = frappe.db.get_value(
		"MS Activity",
		activity,
		["name", "status", "capacity", "requires_consent", "registration_deadline", "title"],
		as_dict=True,
	)
	if not doc:
		return fail(message_en="Activity not found.", message_ar="لم يتم العثور على النشاط.")

	if doc.status != "Open":
		return fail(
			message_en="Registration is not open for this activity.",
			message_ar=f"التسجيل غير متاح — حالة النشاط: {STATUS_AR.get(doc.status, doc.status)}.",
		)
	if doc.registration_deadline and str(doc.registration_deadline) < today():
		return fail(
			message_en="The registration deadline has passed.",
			message_ar=f"انتهى موعد التسجيل بتاريخ {doc.registration_deadline}.",
		)

	# Students and parents may only join an activity aimed at them.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		eligible = _eligible_activities(persona, scope)
		if eligible is not None and activity not in eligible:
			frappe.throw(_("This activity is not open to you."), frappe.PermissionError)

	existing = frappe.db.get_value(
		"MS Activity Enrolment", {"activity": activity, "student": student}, "name"
	)
	if existing:
		row = frappe.get_doc("MS Activity Enrolment", existing)
		if row.status != "Withdrawn":
			return fail(
				message_en="Already registered.",
				message_ar="الطالب مُسجّل في هذا النشاط بالفعل.",
			)
		enrolment = row  # re-joining after withdrawing
	else:
		enrolment = frappe.new_doc("MS Activity Enrolment")
		enrolment.activity = activity
		enrolment.student = student

	# Full activities waitlist rather than refuse, so the office can promote.
	taken = frappe.db.count(
		"MS Activity Enrolment",
		{"activity": activity, "status": ["in", ["Registered", "Confirmed"]]},
	)
	capacity = cint(doc.capacity)
	waitlisted = bool(capacity and taken >= capacity)

	enrolment.status = "Waitlisted" if waitlisted else "Registered"
	enrolment.consent_status = "Pending" if doc.requires_consent else "Not Required"
	enrolment.enrolled_on = now_datetime()
	enrolment.enrolled_by = frappe.session.user
	enrolment.save(ignore_permissions=True)
	frappe.db.commit()

	if waitlisted:
		message_ar = f"النشاط مكتمل — تم وضع الطالب في قائمة الانتظار (المركز {taken - capacity + 1})."
	elif doc.requires_consent:
		message_ar = "تم التسجيل — بانتظار موافقة ولي الأمر."
	else:
		message_ar = "تم التسجيل في النشاط."

	return {
		"success": True,
		"data": {
			"id": enrolment.name,
			"status": enrolment.status,
			"status_label": ENROLMENT_AR.get(enrolment.status),
			"consent_status": enrolment.consent_status,
			"waitlisted": waitlisted,
		},
		"message_en": "Registered.",
		"message_ar": message_ar,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def withdraw(enrolment: str, persona: str = None):
	"""Withdraw a registration, promoting the first waitlisted student."""
	row = frappe.get_doc("MS Activity Enrolment", enrolment)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		scope = resolve_scope(persona)
		if row.student not in (scope.get("students") or []):
			frappe.throw(_("This is not your registration."), frappe.PermissionError)

	was_active = row.status in ("Registered", "Confirmed")
	row.status = "Withdrawn"
	row.save(ignore_permissions=True)

	promoted = _promote_next(row.activity) if was_active else None

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": enrolment, "promoted": promoted},
		"message_en": "Withdrawn.",
		"message_ar": "تم الانسحاب من النشاط."
		+ (f" وتمت ترقية {promoted} من قائمة الانتظار." if promoted else ""),
	}


def _promote_next(activity: str) -> str | None:
	"""Give a freed seat to whoever has waited longest."""
	nxt = frappe.get_all(
		"MS Activity Enrolment",
		filters={"activity": activity, "status": "Waitlisted"},
		fields=["name", "student_name"],
		order_by="enrolled_on asc",
		limit=1,
	)
	if not nxt:
		return None
	doc = frappe.get_doc("MS Activity Enrolment", nxt[0].name)
	doc.status = "Registered"
	doc.save(ignore_permissions=True)
	return nxt[0].student_name


@frappe.whitelist()
@ms_endpoint(ROLE_PARENT, ROLE_ADMIN, ROLE_SECRETARY)
def give_consent(enrolment: str, granted: int = 1, notes: str = None, persona: str = None):
	"""A guardian grants or declines consent for their own child."""
	row = frappe.get_doc("MS Activity Enrolment", enrolment)

	if persona == ROLE_PARENT:
		scope = resolve_scope(persona)
		if row.student not in (scope.get("students") or []):
			frappe.throw(
				_("You can only give consent for your own children."), frappe.PermissionError
			)

	if row.consent_status == "Not Required":
		return fail(
			message_en="This activity does not need consent.",
			message_ar="هذا النشاط لا يحتاج موافقة ولي الأمر.",
		)

	row.consent_status = "Granted" if cint(granted) else "Declined"
	row.consent_by = frappe.session.user
	row.consent_on = now_datetime()
	row.consent_notes = notes
	# Consent is what confirms a place; declining withdraws it.
	promoted = None
	if cint(granted):
		if row.status == "Registered":
			row.status = "Confirmed"
		row.save(ignore_permissions=True)
	else:
		freed = row.status in ("Registered", "Confirmed")
		row.status = "Withdrawn"
		row.save(ignore_permissions=True)
		if freed:
			promoted = _promote_next(row.activity)

	frappe.db.commit()

	message_ar = "تمت موافقة ولي الأمر." if cint(granted) else "تم رفض المشاركة."
	if promoted:
		message_ar += f" وتمت ترقية {promoted} من قائمة الانتظار."

	return {
		"success": True,
		"data": {
			"id": enrolment,
			"consent_status": row.consent_status,
			"status": row.status,
			"promoted": promoted,
		},
		"message_en": "Consent recorded.",
		"message_ar": message_ar,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def participants(activity: str, persona: str = None):
	"""Everyone registered for an activity, for the supervisor's list."""
	doc = frappe.db.get_value(
		"MS Activity",
		activity,
		["name", "title", "capacity", "requires_consent", "start_date"],
		as_dict=True,
	)
	if not doc:
		return fail(message_en="Activity not found.", message_ar="لم يتم العثور على النشاط.")

	rows = frappe.get_all(
		"MS Activity Enrolment",
		filters={"activity": activity},
		fields=[
			"name", "student", "student_name", "status", "consent_status",
			"attended", "enrolled_on", "consent_notes",
		],
		order_by="enrolled_on",
	)

	return {
		"activity": {
			"id": doc.name,
			"title": doc.title,
			"capacity": cint(doc.capacity),
			"requires_consent": bool(doc.requires_consent),
			"start_date": str(doc.start_date or ""),
		},
		"rows": [
			{
				"id": r.name,
				"student": r.student,
				"student_name": r.student_name,
				"status": r.status,
				"status_label": ENROLMENT_AR.get(r.status, r.status),
				"consent_status": r.consent_status,
				"consent_label": CONSENT_AR.get(r.consent_status, r.consent_status),
				"attended": bool(r.attended),
				"enrolled_on": str(r.enrolled_on or ""),
				"notes": r.consent_notes,
			}
			for r in rows
		],
		"confirmed": sum(1 for r in rows if r.status in ("Registered", "Confirmed")),
		"waitlisted": sum(1 for r in rows if r.status == "Waitlisted"),
		"awaiting_consent": sum(1 for r in rows if r.consent_status == "Pending"),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def mark_attendance(entries: str | list, persona: str = None):
	"""Record who actually turned up."""
	rows = parse_json_arg(entries, []) or []
	updated = 0
	for row in rows:
		if not row.get("id"):
			continue
		frappe.db.set_value(
			"MS Activity Enrolment",
			row["id"],
			"attended",
			cint(row.get("attended")),
			update_modified=False,
		)
		updated += 1
	frappe.db.commit()
	return {
		"success": True,
		"data": {"updated": updated},
		"message_en": f"Updated {updated}.",
		"message_ar": f"تم تسجيل حضور {updated} طالباً.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def form_options(persona: str = None):
	"""Dropdown data for the activity form."""
	return {
		"types": [{"code": c, "label": l} for c, l in TYPE_AR.items()],
		"statuses": [{"code": c, "label": l} for c, l in STATUS_AR.items()],
		"programs": frappe.get_all("Program", pluck="name", limit=100),
		"groups": [
			{"id": g.name, "name": g.student_group_name or g.name}
			for g in frappe.get_all(
				"Student Group",
				filters={"disabled": 0},
				fields=["name", "student_group_name"],
				order_by="student_group_name",
				limit=300,
			)
		],
		"supervisors": [
			{"id": i.name, "name": i.instructor_name or i.name}
			for i in frappe.get_all(
				"Instructor", fields=["name", "instructor_name"], limit=200
			)
		],
	}
