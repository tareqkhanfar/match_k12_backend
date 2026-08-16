# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Student health records, clinic visits and behaviour records."""

import frappe
from frappe import _
from frappe.utils import cint, flt, today

from match_schools.api.utils import (
	anchor_term,
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	build_conditions,
	build_order_by,
	fail,
	ms_endpoint,
	paginate,
	parse_json_arg,
	resolve_scope,
)

VISIT_TYPE_AR = {
	"Illness": "مرض",
	"Injury": "إصابة",
	"Medication": "دواء",
	"Checkup": "فحص",
	"Other": "أخرى",
}
OUTCOME_AR = {
	"Returned to Class": "عاد للصف",
	"Sent Home": "أُرسل للمنزل",
	"Referred to Hospital": "حُوّل للمستشفى",
	"Rest in Clinic": "استراحة في العيادة",
}
BEHAVIOUR_TYPE_AR = {"Positive": "إيجابي", "Negative": "سلبي"}


def _visible_students(persona: str) -> list[str] | None:
	"""None means unrestricted; a list restricts to those students."""
	if persona in BACK_OFFICE:
		return None
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		from match_schools.api.students import _students_of_instructor

		return _students_of_instructor(scope.get("instructor"))
	return scope.get("students") or []


def _assert_can_see(persona: str, student: str):
	allowed = _visible_students(persona)
	if allowed is not None and student not in allowed:
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)


# --- Health record ---------------------------------------------------------


def split_categories(value: str | None) -> list[str]:
	"""One record's categories, as a list.

	Stored comma-separated so a single incident can be both "late" and "no
	homework". Older records hold a single value, which is a one-item list.
	"""
	if not value:
		return []
	return [part.strip() for part in str(value).split(",") if part.strip()]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_health_record(student: str, persona: str = None):
	"""Full health profile for one student, including clinic visits."""
	_assert_can_see(persona, student)

	record = frappe.db.get_value(
		"MS Health Record",
		{"student": student},
		[
			"name", "student", "student_name", "blood_group", "height_cm", "weight_kg",
			"chronic_conditions", "allergies", "medications", "special_needs",
			"immunisations", "last_checkup", "emergency_contact_name",
			"emergency_contact_phone", "physician_name", "physician_phone", "notes",
		],
		as_dict=True,
	)

	return {
		"student": student,
		"student_name": frappe.db.get_value("Student", student, "student_name"),
		"record": record,
		"visits": _visits_for(student),
	}


def _visits_for(student: str, limit: int = 30) -> list[dict]:
	rows = frappe.get_all(
		"MS Health Visit",
		filters={"student": student},
		fields=[
			"name", "visit_date", "visit_type", "complaint", "treatment",
			"outcome", "parent_notified",
		],
		order_by="visit_date desc",
		limit=limit,
	)
	return [
		{
			"id": r.name,
			"date": str(r.visit_date or ""),
			"type": VISIT_TYPE_AR.get(r.visit_type, r.visit_type),
			"type_raw": r.visit_type,
			"complaint": r.complaint,
			"treatment": r.treatment,
			"outcome": OUTCOME_AR.get(r.outcome, r.outcome),
			"parent_notified": bool(r.parent_notified),
		}
		for r in rows
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_health_record(payload: str | dict, persona: str = None):
	"""Create or update the health record for a student."""
	data = parse_json_arg(payload) or {}
	student = data.get("student")
	if not student:
		return fail(message_en="Student is required.", message_ar="الطالب مطلوب.")

	fields = {
		k: data.get(k)
		for k in (
			"blood_group", "height_cm", "weight_kg", "chronic_conditions", "allergies",
			"medications", "special_needs", "immunisations", "last_checkup",
			"emergency_contact_name", "emergency_contact_phone", "physician_name",
			"physician_phone", "notes",
		)
		if data.get(k) is not None
	}

	existing = frappe.db.get_value("MS Health Record", {"student": student}, "name")
	if existing:
		doc = frappe.get_doc("MS Health Record", existing)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Health record updated.", "تم تحديث السجل الصحي."
	else:
		doc = frappe.get_doc({"doctype": "MS Health Record", "student": student, **fields})
		doc.insert()
		msg_en, msg_ar = "Health record created.", "تم إنشاء السجل الصحي."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_health_visit(payload: str | dict, persona: str = None):
	"""Log or update a clinic visit."""
	data = parse_json_arg(payload) or {}
	if not data.get("student"):
		return fail(message_en="Student is required.", message_ar="الطالب مطلوب.")

	fields = {
		k: data.get(k)
		for k in (
			"student", "visit_date", "visit_type", "complaint",
			"treatment", "outcome", "parent_notified",
		)
		if data.get(k) is not None
	}

	visit_id = data.get("id") or data.get("name")
	if visit_id:
		doc = frappe.get_doc("MS Health Visit", visit_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Visit updated.", "تم تحديث الزيارة."
	else:
		doc = frappe.get_doc({"doctype": "MS Health Visit", **fields})
		anchor_term(doc, doc.get("visit_date"))
		doc.insert()
		msg_en, msg_ar = "Visit recorded.", "تم تسجيل الزيارة."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_health_visit(visit: str, persona: str = None):
	frappe.delete_doc("MS Health Visit", visit)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": visit},
		"message_en": "Visit deleted.",
		"message_ar": "تم حذف الزيارة.",
	}


# --- Behaviour -------------------------------------------------------------

BEHAVIOUR_FILTERS = {
	"student": "student",
	"record_type": "record_type",
	"category": "category",
	"student_group": "student_group",
	"from_date": "record_date",
	"to_date": "record_date",
	"search": "student_name",
}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_behaviour(
	filters: str | dict = None,
	page: int = 1,
	page_size: int = 25,
	sort_field: str = None,
	sort_order: str = None,
	persona: str = None,
):
	"""Behaviour records the caller may see, with totals."""
	filters = parse_json_arg(filters) or {}
	page, page_size, offset = paginate(page, page_size)

	conditions = ["1=1"]
	params: dict = {}

	allowed = _visible_students(persona)
	if allowed is not None:
		if not allowed:
			return {"items": [], "total": 0, "page": page, "page_size": page_size, "summary": {}}
		conditions.append("student IN %(allowed)s")
		params["allowed"] = allowed

	# Date range needs the two ends mapped onto one column.
	if filters.get("from_date"):
		conditions.append("record_date >= %(from_date)s")
		params["from_date"] = filters.pop("from_date")
	if filters.get("to_date"):
		conditions.append("record_date <= %(to_date)s")
		params["to_date"] = filters.pop("to_date")
	if filters.get("search"):
		conditions.append("student_name LIKE %(search)s")
		params["search"] = f"%{filters.pop('search')}%"

	conditions += build_conditions(filters, BEHAVIOUR_FILTERS, params)
	where = " AND ".join(conditions)

	order_by = build_order_by(
		sort_field, sort_order, BEHAVIOUR_FILTERS, "record_date DESC, creation DESC"
	)

	total = frappe.db.sql(
		f"SELECT COUNT(*) AS total FROM `tabMS Behaviour Record` WHERE {where}",
		params,
		as_dict=True,
	)[0].total

	params["limit"], params["offset"] = page_size, offset
	rows = frappe.db.sql(
		f"""
		SELECT name, student, student_name, record_date, record_type, points,
			category, student_group, description, action_taken, parent_notified
		FROM `tabMS Behaviour Record`
		WHERE {where}
		ORDER BY {order_by}
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		params,
		as_dict=True,
	)

	summary = frappe.db.sql(
		f"""
		SELECT
			SUM(CASE WHEN record_type = 'Positive' THEN 1 ELSE 0 END) AS positive,
			SUM(CASE WHEN record_type = 'Negative' THEN 1 ELSE 0 END) AS negative,
			SUM(points) AS net_points
		FROM `tabMS Behaviour Record`
		WHERE {where}
		""",
		{k: v for k, v in params.items() if k not in ("limit", "offset")},
		as_dict=True,
	)[0]

	return {
		"items": [
			{
				"id": r.name,
				"student": r.student,
				"student_name": r.student_name,
				"date": str(r.record_date or ""),
				"type": BEHAVIOUR_TYPE_AR.get(r.record_type, r.record_type),
				"type_raw": r.record_type,
				"points": cint(r.points),
				"category": r.category,
				"student_group": r.student_group,
				"description": r.description,
				"action_taken": r.action_taken,
				"parent_notified": bool(r.parent_notified),
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
		"summary": {
			"positive": cint(summary.positive),
			"negative": cint(summary.negative),
			"net_points": cint(summary.net_points),
		},
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_behaviour(payload: str | dict, persona: str = None):
	"""Record or update a behaviour entry."""
	data = parse_json_arg(payload) or {}
	if not data.get("student"):
		return fail(message_en="Student is required.", message_ar="الطالب مطلوب.")

	fields = {
		k: data.get(k)
		for k in (
			"student", "record_date", "record_type", "points", "category",
			"student_group", "description", "action_taken", "parent_notified",
		)
		if data.get(k) is not None
	}
	fields.setdefault("record_date", today())

	# A teacher's entry is attributed to them automatically.
	if persona == ROLE_TEACHER:
		scope = resolve_scope(persona)
		if scope.get("instructor"):
			fields["reported_by"] = scope["instructor"]

	record_id = data.get("id") or data.get("name")
	if record_id:
		doc = frappe.get_doc("MS Behaviour Record", record_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Behaviour record updated.", "تم تحديث السجل السلوكي."
	else:
		doc = frappe.get_doc({"doctype": "MS Behaviour Record", **fields})
		anchor_term(doc, doc.get("record_date"))
		doc.insert()
		msg_en, msg_ar = "Behaviour record added.", "تمت إضافة السجل السلوكي."

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "points": cint(doc.points)},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_behaviour(record: str, persona: str = None):
	frappe.delete_doc("MS Behaviour Record", record)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": record},
		"message_en": "Record deleted.",
		"message_ar": "تم حذف السجل.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def behaviour_summary(student: str, persona: str = None):
	"""Points breakdown for one student, for their portal."""
	_assert_can_see(persona, student)

	row = frappe.db.sql(
		"""
		SELECT
			SUM(CASE WHEN record_type = 'Positive' THEN 1 ELSE 0 END) AS positive,
			SUM(CASE WHEN record_type = 'Negative' THEN 1 ELSE 0 END) AS negative,
			SUM(points) AS net_points
		FROM `tabMS Behaviour Record`
		WHERE student = %(student)s
		""",
		{"student": student},
		as_dict=True,
	)[0]

	# A record may carry several categories ("Late Arrival,Homework"), so the
	# split happens here rather than in SQL — grouping on the raw column would
	# invent a category called "Late Arrival,Homework".
	raw_categories = frappe.db.sql(
		"""
		SELECT category, points
		FROM `tabMS Behaviour Record`
		WHERE student = %(student)s AND category IS NOT NULL AND category != ''
		""",
		{"student": student},
		as_dict=True,
	)
	tally: dict[str, dict] = {}
	for r in raw_categories:
		for name in split_categories(r.category):
			bucket = tally.setdefault(name, {"category": name, "count": 0, "points": 0})
			bucket["count"] += 1
			bucket["points"] += cint(r.points)
	by_category = sorted(tally.values(), key=lambda x: -x["count"])

	return {
		"student": student,
		"positive": cint(row.positive),
		"negative": cint(row.negative),
		"net_points": cint(row.net_points),
		"by_category": [
			{"category": r.category, "count": r.count, "points": cint(r.points)} for r in by_category
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def list_health_records(
	filters: str | dict = None,
	search: str = None,
	page: int = 1,
	page_size: int = 20,
	sort_by: str = None,
	sort_order: str = None,
	persona: str = None,
):
	"""School-wide health register — one row per student with a record."""
	parsed = parse_json_arg(filters, {}) or {}
	conditions = {}

	allowed = _visible_students(persona)
	if allowed is not None:
		if not allowed:
			return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}
		conditions["student"] = ["in", allowed]

	for field in ("blood_group", "student"):
		if parsed.get(field):
			conditions[field] = parsed[field]
	if parsed.get("has_conditions"):
		conditions["chronic_conditions"] = ["!=", ""]

	if search:
		matches = frappe.get_all(
			"Student",
			filters={"student_name": ["like", f"%{search}%"]},
			pluck="name",
			limit=200,
		)
		if not matches:
			return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}
		existing = conditions.get("student")
		if existing and existing[0] == "in":
			matches = [m for m in matches if m in existing[1]]
		conditions["student"] = ["in", matches]

	total = frappe.db.count("MS Health Record", conditions)
	page, page_size, offset = paginate(page, page_size)
	rows = frappe.get_all(
		"MS Health Record",
		filters=conditions,
		fields=[
			"name", "student", "student_name", "blood_group", "chronic_conditions",
			"allergies", "medications", "emergency_contact_name", "emergency_contact_phone",
			"modified",
		],
		order_by=build_order_by(
			sort_by,
			sort_order,
			allowed={"student": "student_name", "updated": "modified"},
			default="student_name asc",
		),
		start=offset,
		page_length=page_size,
	)

	visits = {}
	if rows:
		for v in frappe.get_all(
			"MS Health Visit",
			filters={"student": ["in", [r.student for r in rows]]},
			fields=["student"],
		):
			visits[v.student] = visits.get(v.student, 0) + 1

	return {
		"items": [
			{
				"id": r.name,
				"student": r.student,
				"student_name": r.student_name,
				"blood_group": r.blood_group,
				"chronic_conditions": r.chronic_conditions,
				"allergies": r.allergies,
				"medications": r.medications,
				"emergency_contact": r.emergency_contact_name,
				"emergency_phone": r.emergency_contact_phone,
				"visits": visits.get(r.student, 0),
				"updated": str(r.modified or "")[:10],
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
	}
