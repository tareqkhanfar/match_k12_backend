# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Student directory and profile endpoints."""

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	build_order_by,
	fail,
	get_default_academic_year,
	ms_endpoint,
	paginate,
	resolve_scope,
	ROLE_SECRETARY,
)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_PARENT, ROLE_STUDENT)
def list_students(
	search: str = None,
	program: str = None,
	batch: str = None,
	payment_status: str = None,
	page: int = 1,
	page_size: int = 20,
	persona: str = None,
):
	"""Paginated student directory, scoped to what the caller may see."""
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 20, 1), 100)
	scope = resolve_scope(persona)

	allowed = _allowed_student_ids(persona, scope)
	if allowed is not None and not allowed:
		return {"items": [], "total": 0, "page": page, "page_size": page_size}

	conditions = ["s.enabled = 1"]
	params: dict = {}

	if allowed is not None:
		conditions.append("s.name IN %(allowed)s")
		params["allowed"] = allowed

	if search:
		conditions.append("(s.student_name LIKE %(search)s OR s.name LIKE %(search)s)")
		params["search"] = f"%{search}%"

	# Program / batch live on Program Enrollment, so join through it.
	joins = ""
	academic_year = get_default_academic_year()
	if program or batch:
		joins = """
			INNER JOIN `tabProgram Enrollment` pe
				ON pe.student = s.name AND pe.docstatus < 2
		"""
		if program:
			conditions.append("pe.program = %(program)s")
			params["program"] = program
		if batch:
			conditions.append("pe.student_batch_name = %(batch)s")
			params["batch"] = batch
		if academic_year:
			conditions.append("pe.academic_year = %(academic_year)s")
			params["academic_year"] = academic_year

	where = " AND ".join(conditions)

	total = frappe.db.sql(
		f"""
		SELECT COUNT(DISTINCT s.name) AS total
		FROM `tabStudent` s {joins}
		WHERE {where}
		""",
		params,
		as_dict=True,
	)[0].total

	params["limit"] = page_size
	params["offset"] = (page - 1) * page_size
	rows = frappe.db.sql(
		f"""
		SELECT DISTINCT s.name, s.student_name, s.gender, s.image,
			s.student_email_id, s.student_mobile_number, s.date_of_birth,
			s.address_line_1, s.joining_date
		FROM `tabStudent` s {joins}
		WHERE {where}
		ORDER BY s.student_name
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		params,
		as_dict=True,
	)

	items = [_student_row(r) for r in rows]

	# Payment status is derived, so filter after enrichment.
	if payment_status and payment_status != "all":
		items = [i for i in items if i["status"] == payment_status]

	return {"items": items, "total": total, "page": page, "page_size": page_size}


def _allowed_student_ids(persona: str, scope: dict) -> list[str] | None:
	"""Return None for unrestricted access, else the visible student ids."""
	if persona in BACK_OFFICE:
		return None
	if persona == ROLE_TEACHER:
		return _students_of_instructor(scope.get("instructor"))
	# Students and parents only ever see themselves / their children.
	return scope.get("students") or []


def _students_of_instructor(instructor: str | None) -> list[str]:
	if not instructor:
		return []
	groups = [
		r.parent
		for r in frappe.get_all(
			"Student Group Instructor",
			filters={"instructor": instructor, "parenttype": "Student Group"},
			fields=["parent"],
		)
	]
	if not groups:
		return []
	return list(
		{
			r.student
			for r in frappe.get_all(
				"Student Group Student",
				filters={"parent": ["in", groups], "parenttype": "Student Group", "active": 1},
				fields=["student"],
			)
		}
	)


def _student_row(r: dict) -> dict:
	"""Shape a student for the directory table."""
	enrollment = _latest_enrollment(r["name"])
	guardian = _primary_guardian(r["name"])
	fees = _fee_totals(r["name"])

	return {
		"id": r["name"],
		"name": r["student_name"],
		"gender": _gender_ar(r.get("gender")),
		"image": r.get("image"),
		"email": r.get("student_email_id"),
		"phone": r.get("student_mobile_number"),
		"birthDate": str(r.get("date_of_birth") or ""),
		"address": r.get("address_line_1") or "",
		"enrolled": str(r.get("joining_date") or ""),
		"grade": enrollment.get("program") if enrollment else None,
		"section": enrollment.get("student_batch_name") if enrollment else None,
		"guardian": guardian.get("guardian_name") if guardian else None,
		"guardianPhone": guardian.get("mobile_number") if guardian else None,
		"attendanceRate": _attendance_rate(r["name"]),
		"average": _average_score(r["name"]),
		"feeTotal": fees["total"],
		"feePaid": fees["paid"],
		"status": fees["status"],
	}


def _gender_ar(gender: str | None) -> str:
	return {"Male": "ذكر", "Female": "أنثى"}.get(gender, gender or "")


def _latest_enrollment(student: str) -> dict | None:
	rows = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": ["<", 2]},
		fields=["program", "student_batch_name", "academic_year", "academic_term"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _primary_guardian(student: str) -> dict | None:
	rows = frappe.get_all(
		"Student Guardian",
		filters={"parent": student, "parenttype": "Student"},
		fields=["guardian", "guardian_name"],
		order_by="idx",
		limit=1,
	)
	if not rows:
		return None
	mobile = frappe.db.get_value("Guardian", rows[0].guardian, "mobile_number")
	return {
		"guardian": rows[0].guardian,
		"guardian_name": rows[0].guardian_name,
		"mobile_number": mobile,
	}


def _attendance_rate(student: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS total,
			SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance`
		WHERE student = %(student)s AND docstatus < 2
		""",
		{"student": student},
		as_dict=True,
	)
	if not row or not row[0].total:
		return 0.0
	return round(flt(row[0].present) / flt(row[0].total) * 100, 1)


def _average_score(student: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT AVG(total_score / maximum_score * 100) AS average
		FROM `tabAssessment Result`
		WHERE student = %(student)s AND docstatus = 1 AND maximum_score > 0
		""",
		{"student": student},
		as_dict=True,
	)
	return round(flt(row[0].average), 1) if row and row[0].average else 0.0


def _fee_totals(student: str) -> dict:
	row = frappe.db.sql(
		"""
		SELECT SUM(grand_total) AS total, SUM(outstanding_amount) AS outstanding
		FROM `tabFees`
		WHERE student = %(student)s AND docstatus = 1
		""",
		{"student": student},
		as_dict=True,
	)
	total = flt(row[0].total) if row else 0.0
	outstanding = flt(row[0].outstanding) if row else 0.0
	paid = total - outstanding

	if total <= 0:
		status = "paid"
	elif outstanding <= 0:
		status = "paid"
	elif paid > total * 0.3:
		status = "partial"
	else:
		status = "late"

	return {"total": total, "paid": paid, "outstanding": outstanding, "status": status}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_PARENT, ROLE_STUDENT)
def get_student(student: str, persona: str = None):
	"""Full student profile: personal, guardians, academics, attendance, fees."""
	scope = resolve_scope(persona)
	allowed = _allowed_student_ids(persona, scope)
	if allowed is not None and student not in allowed:
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	doc = frappe.db.get_value(
		"Student",
		student,
		[
			"name", "student_name", "gender", "image", "date_of_birth",
			"student_email_id", "student_mobile_number", "joining_date",
			"address_line_1", "city", "nationality", "blood_group", "enabled",
		],
		as_dict=True,
	)
	if not doc:
		return fail(
			message_en="Student not found.",
			message_ar="لم يتم العثور على الطالب.",
		)

	enrollment = _latest_enrollment(student)
	fees = _fee_totals(student)

	return {
		"profile": {
			"id": doc.name,
			"name": doc.student_name,
			"gender": _gender_ar(doc.gender),
			"image": doc.image,
			"birthDate": str(doc.date_of_birth or ""),
			"email": doc.student_email_id,
			"phone": doc.student_mobile_number,
			"enrolled": str(doc.joining_date or ""),
			"address": doc.address_line_1 or "",
			"city": doc.city,
			"nationality": doc.nationality,
			"blood_group": doc.blood_group,
			"active": bool(doc.enabled),
			"grade": enrollment.get("program") if enrollment else None,
			"section": enrollment.get("student_batch_name") if enrollment else None,
			"academic_year": enrollment.get("academic_year") if enrollment else None,
		},
		"guardians": _guardians(student),
		"academics": _academic_records(student),
		"attendance": _attendance_summary(student),
		"fees": {
			"total": fees["total"],
			"paid": fees["paid"],
			"outstanding": fees["outstanding"],
			"status": fees["status"],
			"invoices": _fee_invoices(student),
		},
		"groups": _groups_of_student(student),
	}


def _guardians(student: str) -> list[dict]:
	rows = frappe.get_all(
		"Student Guardian",
		filters={"parent": student, "parenttype": "Student"},
		fields=["guardian", "guardian_name", "relation"],
		order_by="idx",
	)
	out = []
	for r in rows:
		details = (
			frappe.db.get_value(
				"Guardian",
				r.guardian,
				["mobile_number", "email_address", "occupation", "designation"],
				as_dict=True,
			)
			or {}
		)
		out.append(
			{
				"id": r.guardian,
				"name": r.guardian_name,
				"relation": r.relation,
				"phone": details.get("mobile_number"),
				"email": details.get("email_address"),
				"occupation": details.get("occupation"),
			}
		)
	return out


def _academic_records(student: str, limit: int = 20) -> list[dict]:
	rows = frappe.get_all(
		"Assessment Result",
		filters={"student": student, "docstatus": 1},
		fields=[
			"name", "course", "total_score", "maximum_score", "grade",
			"academic_term", "academic_year", "assessment_plan",
		],
		order_by="creation desc",
		limit=limit,
	)
	return [
		{
			"id": r.name,
			"subject": r.course,
			"score": flt(r.total_score),
			"max": flt(r.maximum_score),
			"percentage": round(flt(r.total_score) / flt(r.maximum_score) * 100, 1)
			if flt(r.maximum_score)
			else 0.0,
			"grade": r.grade,
			"term": r.academic_term,
			"year": r.academic_year,
		}
		for r in rows
	]


def _attendance_summary(student: str) -> dict:
	rows = frappe.db.sql(
		"""
		SELECT status, COUNT(*) AS count
		FROM `tabStudent Attendance`
		WHERE student = %(student)s AND docstatus < 2
		GROUP BY status
		""",
		{"student": student},
		as_dict=True,
	)
	counts = {r.status: r.count for r in rows}
	total = sum(counts.values())
	present = counts.get("Present", 0)
	return {
		"present": present,
		"absent": counts.get("Absent", 0),
		"leave": counts.get("Leave", 0),
		"total": total,
		"rate": round(flt(present) / flt(total) * 100, 1) if total else 0.0,
		"recent": _recent_attendance(student),
	}


def _recent_attendance(student: str, limit: int = 15) -> list[dict]:
	rows = frappe.get_all(
		"Student Attendance",
		filters={"student": student, "docstatus": ["<", 2]},
		fields=["name", "date", "status", "student_group"],
		order_by="date desc",
		limit=limit,
	)
	return [
		{"id": r.name, "date": str(r.date), "status": r.status, "group": r.student_group}
		for r in rows
	]


def _fee_invoices(student: str) -> list[dict]:
	rows = frappe.get_all(
		"Fees",
		filters={"student": student, "docstatus": 1},
		fields=[
			"name", "posting_date", "due_date", "grand_total",
			"outstanding_amount", "academic_term", "academic_year", "program",
		],
		order_by="posting_date desc",
	)
	out = []
	for r in rows:
		paid = flt(r.grand_total) - flt(r.outstanding_amount)
		out.append(
			{
				"id": r.name,
				"date": str(r.posting_date or ""),
				"due_date": str(r.due_date or ""),
				"total": flt(r.grand_total),
				"paid": paid,
				"outstanding": flt(r.outstanding_amount),
				"status": "paid" if flt(r.outstanding_amount) <= 0 else ("partial" if paid > 0 else "late"),
				"term": r.academic_term,
				"program": r.program,
			}
		)
	return out


def _groups_of_student(student: str) -> list[dict]:
	names = [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	]
	if not names:
		return []
	return frappe.get_all(
		"Student Group",
		filters={"name": ["in", names]},
		fields=["name", "student_group_name", "program", "batch", "course", "academic_year"],
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_student(payload: str | dict, persona: str = None):
	"""Create or update a student record (admin only)."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	student_id = data.get("id") or data.get("name")
	fields = {
		"first_name": data.get("first_name"),
		"middle_name": data.get("middle_name"),
		"last_name": data.get("last_name"),
		"gender": data.get("gender"),
		"date_of_birth": data.get("date_of_birth"),
		"student_email_id": data.get("email"),
		"student_mobile_number": data.get("phone"),
		"address_line_1": data.get("address"),
		"city": data.get("city"),
		"nationality": data.get("nationality"),
		"joining_date": data.get("joining_date"),
	}
	fields = {k: v for k, v in fields.items() if v is not None}

	if student_id:
		doc = frappe.get_doc("Student", student_id)
		doc.update(fields)
		with _student_user_creation_guard(doc):
			doc.save()
		message_en, message_ar = "Student updated.", "تم تحديث بيانات الطالب."
	else:
		if not fields.get("first_name"):
			return fail(
				message_en="First name is required.",
				message_ar="الاسم الأول مطلوب.",
			)
		# Some sites mark the student email mandatory on the Student doctype.
		# Check up front so the user gets a clear message instead of a 500.
		if _email_is_mandatory() and not fields.get("student_email_id"):
			return fail(
				message_en="Student email is required on this site.",
				message_ar="البريد الإلكتروني للطالب مطلوب.",
			)
		fields["doctype"] = "Student"
		fields.setdefault("enabled", 1)
		doc = frappe.get_doc(fields)
		with _student_user_creation_guard(doc):
			doc.insert()
		message_en, message_ar = "Student created.", "تم إنشاء الطالب."

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "name": doc.student_name},
		"message_en": message_en,
		"message_ar": message_ar,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def filter_options(persona: str = None):
	"""Values for the directory's grade/section dropdowns."""
	programs = frappe.get_all("Program", fields=["name", "program_name"], order_by="name")
	batches = frappe.get_all("Student Batch Name", fields=["name"], order_by="name")
	return {
		"grades": [p.name for p in programs],
		"sections": [b.name for b in batches],
		"statuses": [
			{"value": "paid", "label": "مدفوع"},
			{"value": "partial", "label": "جزئي"},
			{"value": "late", "label": "متأخر"},
		],
	}


@contextmanager
def _student_user_creation_guard(doc):
	"""Stop Education creating a portal User when the student has no email.

	Student.validate_user() checks `frappe.db.exists("User", self.student_email_id)`.
	When the email is empty that check is falsy, so it goes on to insert a User
	with a null name and fails with:

	    AttributeError: 'NoneType' object has no attribute 'strip'

	Students without an email address are perfectly normal in a school, so we
	flip Education's own `user_creation_skip` setting for the duration of the
	save and restore it afterwards.
	"""
	needs_guard = not (doc.get("student_email_id") or "").strip()
	previous = None

	if needs_guard:
		previous = frappe.db.get_single_value("Education Settings", "user_creation_skip")
		frappe.db.set_single_value("Education Settings", "user_creation_skip", 1)

	try:
		yield
	finally:
		if needs_guard:
			frappe.db.set_single_value(
				"Education Settings", "user_creation_skip", previous or 0
			)


def _email_is_mandatory() -> bool:
	"""Whether this site requires student_email_id on Student."""
	field = frappe.get_meta("Student").get_field("student_email_id")
	return bool(field and field.reqd)


# --- Guardians -------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def list_guardians(
	search: str = None,
	page: int = 1,
	page_size: int = 20,
	sort_by: str = None,
	sort_order: str = None,
	persona: str = None,
):
	"""Guardians with the children linked to each."""
	filters = {}
	if search:
		filters["guardian_name"] = ["like", f"%{search}%"]

	total = frappe.db.count("Guardian", filters)
	page, page_size, offset = paginate(page, page_size)
	rows = frappe.get_all(
		"Guardian",
		filters=filters,
		fields=[
			"name", "guardian_name", "email_address", "mobile_number",
			"alternate_number", "occupation", "designation", "user", "image",
		],
		order_by=build_order_by(
			sort_by,
			sort_order,
			allowed={"name": "guardian_name", "created": "creation"},
			default="guardian_name asc",
		),
		start=offset,
		page_length=page_size,
	)

	# One query for every link, rather than one per guardian.
	children: dict[str, list] = {}
	if rows:
		for link in frappe.get_all(
			"Student Guardian",
			filters={"guardian": ["in", [r.name for r in rows]], "parenttype": "Student"},
			fields=["guardian", "parent", "relation"],
		):
			name = frappe.db.get_value("Student", link.parent, "student_name")
			children.setdefault(link.guardian, []).append(
				{"id": link.parent, "name": name or link.parent, "relation": link.relation}
			)

	return {
		"items": [
			{
				"id": r.name,
				"name": r.guardian_name,
				"email": r.email_address,
				"phone": r.mobile_number,
				"alternate_phone": r.alternate_number,
				"occupation": r.occupation,
				"designation": r.designation,
				"user": r.user,
				"image": r.image,
				"children": children.get(r.name, []),
				"children_count": len(children.get(r.name, [])),
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_guardian(payload: str | dict, persona: str = None):
	"""Create or update a guardian."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	fields = {
		"guardian_name": data.get("name") or data.get("guardian_name"),
		"email_address": data.get("email"),
		"mobile_number": data.get("phone"),
		"alternate_number": data.get("alternate_phone"),
		"occupation": data.get("occupation"),
		"designation": data.get("designation"),
	}
	fields = {k: v for k, v in fields.items() if v is not None}

	guardian_id = data.get("id")
	if guardian_id:
		doc = frappe.get_doc("Guardian", guardian_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Guardian updated.", "تم تحديث ولي الأمر."
	else:
		if not fields.get("guardian_name"):
			return fail(message_en="Name is required.", message_ar="الاسم مطلوب.")
		if fields.get("email_address") and frappe.db.exists(
			"Guardian", {"email_address": fields["email_address"]}
		):
			return fail(
				message_en="A guardian with this email already exists.",
				message_ar="يوجد ولي أمر بنفس البريد الإلكتروني.",
			)
		fields["doctype"] = "Guardian"
		doc = frappe.get_doc(fields)
		doc.insert()
		msg_en, msg_ar = "Guardian created.", "تم إنشاء ولي الأمر."

	if data.get("id_number"):
		frappe.db.set_value("Guardian", doc.name, "ms_id_number", data["id_number"])

	# A guardian needs a login to follow their child. It is created here, once,
	# and the password comes back so the registrar can hand it over — it is
	# never readable again.
	credentials = None
	if not doc.get("user"):
		from match_schools.api.credentials import create_account

		credentials = create_account(
			ROLE_PARENT, doc.name, doc.guardian_name, mobile=doc.get("mobile_number")
		)
		frappe.db.set_value("Guardian", doc.name, "user", credentials["user"])

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "name": doc.guardian_name, "credentials": credentials},
		"message_en": msg_en,
		"message_ar": msg_ar,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_guardian(guardian: str, persona: str = None):
	"""Remove a guardian, provided no student still points at them."""
	linked = frappe.db.count(
		"Student Guardian", {"guardian": guardian, "parenttype": "Student"}
	)
	if linked:
		return fail(
			message_en=f"This guardian is still linked to {linked} student(s).",
			message_ar=f"ولي الأمر مرتبط بـ {linked} طالب/طلاب، يجب فك الارتباط أولاً.",
		)
	frappe.delete_doc("Guardian", guardian)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": guardian},
		"message_en": "Guardian deleted.",
		"message_ar": "تم حذف ولي الأمر.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def link_guardian(student: str, guardian: str, relation: str = None, persona: str = None):
	"""Attach a guardian to a student."""
	if not frappe.db.exists("Student", student):
		return fail(message_en="Student not found.", message_ar="لم يتم العثور على الطالب.")
	if not frappe.db.exists("Guardian", guardian):
		return fail(message_en="Guardian not found.", message_ar="لم يتم العثور على ولي الأمر.")

	doc = frappe.get_doc("Student", student)
	if any(g.guardian == guardian for g in doc.guardians):
		return fail(
			message_en="This guardian is already linked to the student.",
			message_ar="ولي الأمر مرتبط بالطالب بالفعل.",
		)

	doc.append("guardians", {"guardian": guardian, "relation": relation})
	with _student_user_creation_guard(doc):
		doc.save()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"student": student, "guardian": guardian},
		"message_en": "Guardian linked.",
		"message_ar": "تم ربط ولي الأمر بالطالب.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def unlink_guardian(student: str, guardian: str, persona: str = None):
	"""Detach a guardian from a student."""
	doc = frappe.get_doc("Student", student)
	remaining = [g for g in doc.guardians if g.guardian != guardian]
	if len(remaining) == len(doc.guardians):
		return fail(
			message_en="This guardian is not linked to the student.",
			message_ar="ولي الأمر غير مرتبط بهذا الطالب.",
		)

	doc.set("guardians", [])
	for g in remaining:
		doc.append("guardians", {"guardian": g.guardian, "relation": g.relation})
	with _student_user_creation_guard(doc):
		doc.save()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"student": student, "guardian": guardian},
		"message_en": "Guardian unlinked.",
		"message_ar": "تم فك ارتباط ولي الأمر.",
	}
