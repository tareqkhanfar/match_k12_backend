
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Admissions: from application to an enrolled student with a login.

An applicant moves Applied → Approved → Admitted (or Rejected). Admitting is
the step that matters: it creates the Student record, the guardians, and the
logins for all of them, and hands the credentials back once so the registrar
can print them. Nothing here stores a readable password.
"""

import frappe
from frappe import _
from frappe.utils import cint, today

from match_schools.api.credentials import create_account, credentials_for
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	build_order_by,
	fail,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

STATUS_AR = {
	"Applied": "مقدَّم",
	"Approved": "مقبول",
	"Rejected": "مرفوض",
	"Admitted": "مُسجَّل",
}
STATUS_TONE = {
	"Applied": "warning",
	"Approved": "info",
	"Rejected": "danger",
	"Admitted": "success",
}

# Which status a transition may move to, and from where. Mirrors the Workflow
# so the API refuses an invalid jump even if called directly.
ALLOWED_MOVES = {
	"Applied": {"Approved", "Rejected"},
	"Approved": {"Admitted", "Rejected"},
	"Rejected": {"Applied"},
	"Admitted": set(),
}

# Doctype field -> the key the form sends. Every writable field on Student
# Applicant appears here, so the registration form can carry the whole record
# rather than a subset that has to be completed later in the desk.
FIELD_MAP = {
	"first_name": "firstName",
	"middle_name": "middleName",
	"last_name": "lastName",
	"program": "program",
	"academic_term": "academicTerm",
	"student_admission": "studentAdmission",
	"student_category": "studentCategory",
	"student_email_id": "email",
	"student_mobile_number": "mobile",
	"date_of_birth": "birthDate",
	"gender": "gender",
	"blood_group": "bloodGroup",
	"nationality": "nationality",
	"image": "image",
	"address_line_1": "addressLine1",
	"address_line_2": "addressLine2",
	"city": "city",
	"state": "state",
	"pincode": "pincode",
	"country": "country",
}

LIST_FIELDS = [
	"name",
	"first_name",
	"middle_name",
	"last_name",
	"title",
	"ms_id_number",
	"application_status",
	"application_date",
	"program",
	"academic_year",
	"academic_term",
	"student_email_id",
	"student_mobile_number",
	"date_of_birth",
	"gender",
	"nationality",
	"image",
]


def _row(doc: dict) -> dict:
	status = doc.get("application_status") or "Applied"
	return {
		"id": doc.get("name"),
		"name": doc.get("title")
		or " ".join(filter(None, [doc.get("first_name"), doc.get("middle_name"), doc.get("last_name")])),
		"idNumber": doc.get("ms_id_number"),
		"status": status,
		"statusLabel": STATUS_AR.get(status, status),
		"statusTone": STATUS_TONE.get(status, "muted"),
		"appliedOn": str(doc.get("application_date") or ""),
		"program": doc.get("program"),
		"academicYear": doc.get("academic_year"),
		"academicTerm": doc.get("academic_term"),
		"email": doc.get("student_email_id"),
		"mobile": doc.get("student_mobile_number"),
		"birthDate": str(doc.get("date_of_birth") or ""),
		"gender": doc.get("gender"),
		"nationality": doc.get("nationality"),
		"image": doc.get("image"),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def list_applicants(
	search: str = None,
	status: str = None,
	program: str = None,
	academic_year: str = None,
	page: int = 1,
	page_size: int = 20,
	sort_by: str = None,
	sort_dir: str = None,
	persona: str = None,
):
	"""Paginated applicant list with the counts the screen shows as tabs."""
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 20, 1), 100)

	filters: dict = {}
	if status and status != "All":
		filters["application_status"] = status
	if program:
		filters["program"] = program
	if academic_year:
		filters["academic_year"] = academic_year

	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [
			["title", "like", like],
			["first_name", "like", like],
			["last_name", "like", like],
			["ms_id_number", "like", like],
			["name", "like", like],
		]

	order_by = build_order_by(
		sort_by,
		sort_dir,
		allowed={
			"name": "title",
			"status": "application_status",
			"appliedOn": "application_date",
			"program": "program",
		},
		default="modified desc",
	)

	total = frappe.db.count("Student Applicant", filters=filters)
	rows = frappe.get_all(
		"Student Applicant",
		filters=filters,
		or_filters=or_filters,
		fields=LIST_FIELDS,
		order_by=order_by,
		start=(page - 1) * page_size,
		page_length=page_size,
	)

	counts = {"All": frappe.db.count("Student Applicant")}
	for key in STATUS_AR:
		counts[key] = frappe.db.count("Student Applicant", {"application_status": key})

	return {
		"items": [_row(r) for r in rows],
		"total": total,
		"page": page,
		"page_size": page_size,
		"counts": counts,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def get_applicant(applicant: str, persona: str = None):
	"""One applicant, with guardians, siblings and any account already made."""
	if not frappe.db.exists("Student Applicant", applicant):
		return fail(_("Applicant not found"), "لم يتم العثور على الطلب")

	doc = frappe.get_doc("Student Applicant", applicant)
	data = _row(doc.as_dict())

	data["guardians"] = [
		{
			"guardian": g.guardian,
			"name": g.guardian_name,
			"relation": g.relation,
		}
		for g in (doc.get("guardians") or [])
	]
	data["siblings"] = [
		{
			"name": s.full_name,
			"birthDate": str(s.date_of_birth or ""),
			"gender": s.get("gender"),
			"sameSchool": bool(s.studying_in_same_institute),
		}
		for s in (doc.get("siblings") or [])
	]

	# Every remaining writable field, so the edit form opens fully populated.
	for field, key in FIELD_MAP.items():
		if key not in data:
			value = doc.get(field)
			data[key] = str(value) if value is not None else None

	data["address"] = {
		"line1": doc.get("address_line_1"),
		"line2": doc.get("address_line_2"),
		"city": doc.get("city"),
		"state": doc.get("state"),
		"country": doc.get("country"),
	}

	# If this applicant already became a Student, surface the link and login.
	student = frappe.db.get_value(
		"Student", {"student_applicant": applicant}, ["name", "user"], as_dict=True
	)
	data["student"] = student.name if student else None
	data["credentials"] = credentials_for(student.user) if student and student.user else None
	data["allowedMoves"] = sorted(ALLOWED_MOVES.get(data["status"], set()))

	return data


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def form_options(persona: str = None):
	"""Everything the applicant form needs to populate its selects."""
	blood_groups = frappe.get_meta("Student Applicant").get_field("blood_group")
	return {
		"programs": frappe.get_all("Program", pluck="name", order_by="name"),
		"academicYears": frappe.get_all("Academic Year", pluck="name", order_by="year_start_date desc"),
		"academicTerms": frappe.get_all(
			"Academic Term", fields=["name", "academic_year"], order_by="name"
		),
		"genders": frappe.get_all("Gender", pluck="name", order_by="name"),
		"studentCategories": frappe.get_all("Student Category", pluck="name", order_by="name"),
		"studentAdmissions": frappe.get_all("Student Admission", pluck="name", order_by="name"),
		"countries": frappe.get_all("Country", pluck="name", order_by="name"),
		"bloodGroups": [b for b in (blood_groups.options or "").split("\n") if b],
		"guardians": frappe.get_all(
			"Guardian", fields=["name", "guardian_name"], order_by="guardian_name", limit=500
		),
		"defaultAcademicYear": get_default_academic_year(),
		"statuses": [
			{"value": k, "label": v, "tone": STATUS_TONE[k]} for k, v in STATUS_AR.items()
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_applicant(payload: str | dict, persona: str = None):
	"""Create or update an application."""
	data = parse_json_arg(payload) or {}
	applicant_id = data.get("id")

	id_number = (data.get("idNumber") or "").strip()
	if not id_number:
		return fail("ID number is required", "رقم الهوية مطلوب")

	# A national id identifies a person; two applications under one id is
	# almost always a duplicate entry rather than two children.
	clash = frappe.db.get_value(
		"Student Applicant",
		{"ms_id_number": id_number, "name": ["!=", applicant_id or ""]},
		["name", "title"],
		as_dict=True,
	)
	if clash:
		return fail(
			"ID number already used by {0}".format(clash.name),
			"رقم الهوية مستخدم مسبقاً في الطلب {0} ({1})".format(clash.name, clash.title or ""),
		)

	doc = (
		frappe.get_doc("Student Applicant", applicant_id)
		if applicant_id and frappe.db.exists("Student Applicant", applicant_id)
		else frappe.new_doc("Student Applicant")
	)

	if doc.get("application_status") == "Admitted":
		return fail(
			"An admitted application cannot be edited",
			"لا يمكن تعديل طلب تم تسجيله",
		)

	# Every field the doctype carries, keyed by the name the form sends.
	for field, key in FIELD_MAP.items():
		if key in data:
			setattr(doc, field, data.get(key) or None)

	doc.ms_id_number = id_number
	doc.academic_year = data.get("academicYear") or get_default_academic_year()
	if not doc.application_date:
		doc.application_date = today()
	if not doc.application_status:
		doc.application_status = "Applied"

	guardians = parse_json_arg(data.get("guardians"), []) or []
	doc.set("guardians", [])
	for g in guardians:
		if not g.get("guardian"):
			continue
		doc.append("guardians", {"guardian": g["guardian"], "relation": g.get("relation")})

	siblings = parse_json_arg(data.get("siblings"), []) or []
	doc.set("siblings", [])
	for s in siblings:
		if not s.get("name"):
			continue
		doc.append(
			"siblings",
			{
				"full_name": s.get("name"),
				"date_of_birth": s.get("birthDate") or None,
				"gender": s.get("gender") or None,
				"studying_in_same_institute": 1 if s.get("sameSchool") else 0,
			},
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {"id": doc.name, "status": doc.application_status}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def transition(applicant: str, to_status: str, reason: str = None, persona: str = None):
	"""Move an application between states, refusing invalid jumps."""
	if not frappe.db.exists("Student Applicant", applicant):
		return fail(_("Applicant not found"), "لم يتم العثور على الطلب")

	doc = frappe.get_doc("Student Applicant", applicant)
	current = doc.application_status or "Applied"

	if to_status == "Admitted":
		return fail(
			"Use the admit action, which also creates the student",
			"استخدم إجراء التسجيل، فهو ينشئ الطالب وحسابه",
		)

	if to_status not in ALLOWED_MOVES.get(current, set()):
		return fail(
			"Cannot move from {0} to {1}".format(current, to_status),
			"لا يمكن الانتقال من «{0}» إلى «{1}»".format(
				STATUS_AR.get(current, current), STATUS_AR.get(to_status, to_status)
			),
		)

	doc.application_status = to_status
	doc.save(ignore_permissions=True)

	if reason:
		doc.add_comment("Comment", text=reason)
	frappe.db.commit()

	return {"id": doc.name, "status": to_status, "statusLabel": STATUS_AR.get(to_status)}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def admit(applicant: str, persona: str = None):
	"""Turn an approved application into a Student, with logins.

	Returns the credentials for the student and for each guardian that gained
	an account, which is the only moment the passwords are readable.
	"""
	if not frappe.db.exists("Student Applicant", applicant):
		return fail(_("Applicant not found"), "لم يتم العثور على الطلب")

	doc = frappe.get_doc("Student Applicant", applicant)

	if doc.application_status == "Admitted":
		return fail("Already admitted", "تم تسجيل هذا الطلب مسبقاً")
	if doc.application_status != "Approved":
		return fail(
			"Only an approved application can be admitted",
			"يجب قبول الطلب أولاً قبل التسجيل",
		)

	existing = frappe.db.exists("Student", {"student_applicant": applicant})
	if existing:
		return fail(
			"A student already exists for this application",
			"يوجد طالب مرتبط بهذا الطلب مسبقاً",
		)

	full_name = doc.title or " ".join(
		filter(None, [doc.first_name, doc.middle_name, doc.last_name])
	)

	student = frappe.new_doc("Student")
	student.first_name = doc.first_name
	student.middle_name = doc.middle_name
	student.last_name = doc.last_name
	student.student_applicant = applicant
	student.date_of_birth = doc.date_of_birth
	student.gender = doc.gender
	student.nationality = doc.nationality
	student.student_email_id = doc.student_email_id
	student.student_mobile_number = doc.student_mobile_number
	student.ms_id_number = doc.get("ms_id_number")
	student.blood_group = doc.get("blood_group")
	student.student_category = doc.get("student_category")
	student.address_line_1 = doc.get("address_line_1")
	student.address_line_2 = doc.get("address_line_2")
	student.city = doc.get("city")
	student.state = doc.get("state")
	student.pincode = doc.get("pincode")
	if doc.get("image"):
		student.image = doc.get("image")
	student.joining_date = today()
	for s in doc.get("siblings") or []:
		student.append(
			"siblings",
			{
				"full_name": s.full_name,
				"date_of_birth": s.date_of_birth,
				"gender": s.get("gender"),
				"studying_in_same_institute": s.studying_in_same_institute,
			},
		)
	for g in doc.get("guardians") or []:
		student.append("guardians", {"guardian": g.guardian, "relation": g.relation})
	student.insert(ignore_permissions=True)

	# The student's own login.
	account = create_account(
		"student",
		student.name,
		full_name,
		year=doc.academic_year,
		mobile=doc.student_mobile_number,
	)
	frappe.db.set_value("Student", student.name, "user", account["user"], update_modified=False)
	frappe.db.set_value(
		"Student Applicant", applicant, "ms_username", account["username"], update_modified=False
	)

	# Guardians: one login each, so a parent can follow their child.
	guardian_accounts = []
	for row in doc.get("guardians") or []:
		guardian_accounts.append(_ensure_guardian_account(row.guardian, doc.academic_year))

	doc.db_set("application_status", "Admitted", update_modified=False)
	frappe.db.commit()

	return {
		"student": student.name,
		"studentName": full_name,
		"status": "Admitted",
		"credentials": account,
		"guardians": [g for g in guardian_accounts if g],
	}


def _ensure_guardian_account(guardian: str, year: str | None) -> dict | None:
	"""Give a guardian a login if they do not already have one."""
	if not guardian or not frappe.db.exists("Guardian", guardian):
		return None

	row = frappe.db.get_value(
		"Guardian", guardian, ["name", "guardian_name", "user", "mobile_number"], as_dict=True
	)
	if row.user and frappe.db.exists("User", row.user):
		existing = credentials_for(row.user)
		if existing:
			existing["guardian"] = guardian
			existing["isNew"] = False
		return existing

	account = create_account(
		"parent", guardian, row.guardian_name or guardian, year=year, mobile=row.mobile_number
	)
	frappe.db.set_value("Guardian", guardian, "user", account["user"], update_modified=False)
	account["guardian"] = guardian
	account["isNew"] = True
	return account


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_applicant(applicant: str, persona: str = None):
	"""Remove an application that never became a student."""
	if not frappe.db.exists("Student Applicant", applicant):
		return fail(_("Applicant not found"), "لم يتم العثور على الطلب")

	if frappe.db.exists("Student", {"student_applicant": applicant}):
		return fail(
			"This application produced a student and cannot be deleted",
			"لا يمكن حذف طلب أصبح طالباً — احذف الطالب أولاً",
		)

	frappe.delete_doc("Student Applicant", applicant, ignore_permissions=True)
	frappe.db.commit()
	return {"id": applicant}
