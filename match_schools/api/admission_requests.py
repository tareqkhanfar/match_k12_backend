"""Admission requests («طلب متسع»): what comes before an application.

A family asks to enrol a child. The office writes it down as a draft, confirms
it, and prints the admission-approval letter. Moving it on creates the
application — pending, neither accepted nor refused, which is decided there —
carrying what the two share. The request keeps a link to the application it
became, and stops being editable.
"""

import frappe
from frappe.utils import cint, now_datetime, nowdate

from match_schools.api import admission_request_form as letter
from match_schools.api import admissions, forms, forms_print, print_designs
from match_schools.api.utils import (
	BACK_OFFICE,
	fail,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

DOCTYPE = "MS Admission Request"
STATUS_AR = {"Draft": "مسودة", "Confirmed": "مؤكَّد", "Transferred": "رُحِّل إلى طلب التحاق", "Cancelled": "ملغى"}
STATUS_TONE = {"Draft": "warning", "Confirmed": "info", "Transferred": "success", "Cancelled": "muted"}
TEXT_FIELDS = {
	"firstName": "first_name", "middleName": "middle_name", "grandfatherName": "grandfather_name",
	"lastName": "last_name", "idNumber": "id_number", "gender": "gender", "program": "program",
	"academicYear": "academic_year", "guardianName": "guardian_name", "guardianMobile": "guardian_mobile",
	"mobile": "mobile", "email": "email", "city": "city", "previousSchool": "previous_school",
	"principal": "principal", "notes": "notes",
}


def _full_name(d) -> str:
	return " ".join(x for x in (d.first_name, d.middle_name, d.grandfather_name, d.last_name) if x and x.strip())


def _row(d) -> dict:
	return {
		"id": d.name,
		"name": d.applicant_name or _full_name(d),
		"status": d.status,
		"statusLabel": STATUS_AR.get(d.status, d.status),
		"statusTone": STATUS_TONE.get(d.status, "muted"),
		"program": d.program or "",
		"academicYear": d.academic_year or "",
		"idNumber": d.id_number or "",
		"guardianName": d.guardian_name or "",
		"guardianMobile": d.guardian_mobile or "",
		"application": d.get("application") or "",
		"created": str(d.creation)[:10],
		"modified": str(d.modified),
	}


def _detail(d) -> dict:
	return {
		**_row(d),
		**{k: (d.get(v) or "") for k, v in TEXT_FIELDS.items()},
		"birthDate": str(d.birth_date or ""),
		"letterDate": str(d.letter_date or ""),
		"documents": [x for x in (d.documents or "").split("\n") if x.strip()],
		"confirmedOn": str(d.get("confirmed_on") or ""),
		"transferredOn": str(d.get("transferred_on") or ""),
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def options(persona: str = None):
	meta = frappe.get_meta("Student Applicant")
	return {
		"programs": frappe.get_all("Program", pluck="name", order_by="name"),
		"academicYears": frappe.get_all("Academic Year", pluck="name", order_by="year_start_date desc"),
		"defaultAcademicYear": get_default_academic_year(),
		"genders": frappe.get_all("Gender", pluck="name", order_by="name"),
		"documents": letter.DOCUMENTS,
		"statuses": [{"value": k, "label": v, "tone": STATUS_TONE[k]} for k, v in STATUS_AR.items()],
		"requiredForApplication": ["firstName", "idNumber", "program", "academicYear"],
		"fieldOptions": {"bloodGroups": [b for b in (meta.get_field("blood_group").options or "").split("\n") if b]},
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def list_requests(status: str = None, search: str = None, persona: str = None):
	filters = {"status": status} if status else {}
	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [["applicant_name", "like", like], ["id_number", "like", like], ["guardian_name", "like", like], ["name", "like", like]]
	rows = frappe.get_all(
		DOCTYPE, filters=filters, or_filters=or_filters, fields=["*"], order_by="modified desc", limit_page_length=500
	)
	return {
		"requests": [_row(r) for r in rows],
		"counts": {s: frappe.db.count(DOCTYPE, {"status": s}) for s in STATUS_AR},
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def get_request(request: str = None, persona: str = None):
	if not request or not frappe.db.exists(DOCTYPE, request):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	return _detail(frappe.get_doc(DOCTYPE, request))


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_request(payload: str | dict = None, persona: str = None):
	"""Write or amend a request. Only a draft is edited; reopen a confirmed one first."""
	data = parse_json_arg(payload) or {}
	if not (data.get("firstName") or "").strip():
		return fail("The first name is required.", "الاسم الأول مطلوب.")

	if data.get("id"):
		if not frappe.db.exists(DOCTYPE, data["id"]):
			return fail("Not found.", "لم يتم العثور على الطلب.")
		doc = frappe.get_doc(DOCTYPE, data["id"])
		if doc.status != "Draft":
			return fail("Only a draft can be edited.", "لا يُعدَّل إلا الطلب المسودة — أعِد فتح المؤكَّد أولًا.")
	else:
		doc = frappe.new_doc(DOCTYPE)
		doc.status = "Draft"

	for key, field in TEXT_FIELDS.items():
		if key in data:
			doc.set(field, (data.get(key) or "").strip() if isinstance(data.get(key), str) else data.get(key))
	doc.birth_date = data.get("birthDate") or None
	doc.letter_date = data.get("letterDate") or None
	doc.academic_year = doc.academic_year or get_default_academic_year()
	if "documents" in data:
		doc.documents = "\n".join(x.strip() for x in (data.get("documents") or []) if str(x).strip())
	doc.applicant_name = _full_name(doc)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_request(request: str = None, persona: str = None):
	if not request or not frappe.db.exists(DOCTYPE, request):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	if frappe.db.get_value(DOCTYPE, request, "status") in ("Confirmed", "Transferred"):
		return fail("Only a draft or cancelled request is deleted.", "لا يُحذف إلا الطلب المسودة أو الملغى.")
	frappe.delete_doc(DOCTYPE, request, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": request}


def _move(request: str, allowed: tuple[str, ...], to: str, message_ar: str):
	if not request or not frappe.db.exists(DOCTYPE, request):
		return None, fail("Not found.", "لم يتم العثور على الطلب.")
	doc = frappe.get_doc(DOCTYPE, request)
	if doc.status not in allowed:
		return None, fail("Not possible from this state.", message_ar)
	return doc, None


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def confirm_request(request: str = None, persona: str = None):
	"""Confirm a draft: it is final enough to print and to move on."""
	doc, err = _move(request, ("Draft",), "Confirmed", "يُؤكَّد الطلب المسودة فقط.")
	if err:
		return err
	missing = [
		label
		for label, value in (("الاسم الأول", doc.first_name), ("الصف المطلوب", doc.program), ("العام الدراسي", doc.academic_year))
		if not (value or "").strip()
	]
	if missing:
		return fail("Required details are missing.", "أكمل البيانات قبل التأكيد: " + "، ".join(missing) + ".")
	doc.status = "Confirmed"
	doc.confirmed_on = now_datetime()
	doc.confirmed_by = frappe.session.user
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def reopen_request(request: str = None, persona: str = None):
	"""Back to a draft, to correct it."""
	doc, err = _move(request, ("Confirmed", "Cancelled"), "Draft", "لا يُعاد فتح إلا الطلب المؤكَّد أو الملغى.")
	if err:
		return err
	doc.status = "Draft"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def cancel_request(request: str = None, persona: str = None):
	doc, err = _move(request, ("Draft", "Confirmed"), "Cancelled", "لا يُلغى الطلب المرحَّل — الطلب صار طلب التحاق.")
	if err:
		return err
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def to_application(request: str = None, persona: str = None):
	"""Move a confirmed request on as an application, pending.

	It goes through the application's own save, so the same rules apply — an ID
	number already used by another application is refused here as there — and
	the new application starts as «مقدَّم»: accepting or refusing is its job.
	"""
	doc, err = _move(request, ("Confirmed",), "Transferred", "يُرحَّل الطلب المؤكَّد فقط — أكّده أولًا.")
	if err:
		return err
	missing = [
		label
		for label, value in (("رقم الهوية", doc.id_number), ("الصف المطلوب", doc.program), ("العام الدراسي", doc.academic_year))
		if not (value or "").strip()
	]
	if missing:
		return fail(
			"Details needed by the application are missing.",
			"يلزم لطلب الالتحاق: " + "، ".join(missing) + " — أعِد فتح الطلب وأكمله.",
		)
	res = admissions.save_applicant.__wrapped__(
		payload={
			"firstName": doc.first_name, "middleName": doc.middle_name,
			"grandfatherName": doc.grandfather_name, "lastName": doc.last_name,
			"idNumber": doc.id_number, "birthDate": str(doc.birth_date or "") or None,
			"gender": doc.gender or None, "program": doc.program, "academicYear": doc.academic_year,
			"mobile": doc.mobile or doc.guardian_mobile, "email": doc.email,
			"addressLine1": doc.city, "city": doc.city, "guardians": [], "siblings": [],
		},
		persona=None,
	)
	# Reached through Frappe's wrapper it answers in the usual envelope — a
	# failure with its own message, or the result under `data`.
	if isinstance(res, dict) and res.get("success") is False:
		return res
	result = res.get("data", res) if isinstance(res, dict) else {}
	application = (result or {}).get("id")
	if not application:
		return fail("The application was not created.", "تعذّر إنشاء طلب الالتحاق.")
	doc.application = application
	doc.status = "Transferred"
	doc.transferred_on = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


# --- The letter ------------------------------------------------------------------


PRINT_KEY = "admission_request"


def _form_template():
	"""The letter's form — its fields: the school's own «متسع» form if it has
	one, else the built-in letter."""
	name = frappe.db.get_value("MS Form Template", {"title": letter.TITLE, "category": "school"}, "name")
	if name:
		return frappe.get_doc("MS Form Template", name)
	doc = frappe.new_doc("MS Form Template")
	doc.title = letter.TITLE
	doc.category = "school"
	doc.entry_for = "General"
	doc.print_theme = "letter"
	doc.print_orientation = "Portrait"
	doc.print_template = letter.DESIGN
	doc.print_css = letter.CSS
	for fieldname, label, fieldtype, options, default, reqd, width, description in letter.FIELDS:
		doc.append(
			"fields_table",
			{
				"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "options": options,
				"default_value": default, "reqd": reqd, "width": width, "description": description,
			},
		)
	return doc


def _template(design=None):
	"""The letter's form carrying the design it prints with: the one set in
	Settings › تصاميم الطباعة, else the school's own form's, else the built-in
	(see print_designs.effective) — or `design`, a draft being previewed."""
	return print_designs.apply_to_form(_form_template(), design or print_designs.effective(PRINT_KEY))


def letter_html(doc, design=None) -> str:
	template = _template(design)
	entry = frappe.new_doc("MS Form Entry")
	entry.template = template.name if not template.is_new() else None
	entry.filled_on = str(now_datetime())
	female = (doc.gender or "") in ("Female", "أنثى")
	year = (doc.academic_year or "").replace("-", "/")
	answers = {
		"letter_date": str(doc.letter_date or nowdate()),
		"student_word": "الطالبة" if female else "الطالب",
		"applicant": _full_name(doc),
		"grade": doc.program or "",
		"year": year,
		"documents": doc.documents or "",
		"principal": doc.principal or "",
	}
	by_name = {f["fieldname"]: f for f in forms._field_rows(template)}
	for fieldname, value in answers.items():
		f = by_name.get(fieldname)
		if f and value:
			entry.append(
				"values_table",
				{"fieldname": fieldname, "label": f["label"], "fieldtype": f["fieldtype"], "value": str(value)},
			)
	return forms_print.render(template, entry, forms._field_rows(template), False)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def preview(request: str = None, persona: str = None):
	"""The letter as printable HTML — the page the printer gets."""
	if not request or not frappe.db.exists(DOCTYPE, request):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	doc = frappe.get_doc(DOCTYPE, request)
	try:
		html = letter_html(doc)
	except Exception as exc:
		frappe.clear_messages()
		return fail("The print design could not be rendered.", forms_print.error_text(exc))
	return {"html": html, "title": f"{letter.TITLE} — {doc.applicant_name}"}


# --- For the print-design editor ---------------------------------------------------


def print_fields() -> list[dict]:
	"""The letter's fields, for the editor's list of what a design can name."""
	return forms._field_rows(_form_template())


def print_sample(design) -> str:
	"""The letter drawn with `design` on the latest request, or on a made-up
	one when there is none yet."""
	name = frappe.db.get_value(DOCTYPE, {}, "name", order_by="modified desc")
	if name:
		return letter_html(frappe.get_doc(DOCTYPE, name), design)
	doc = frappe.new_doc(DOCTYPE)
	doc.update(
		{
			"first_name": "أحمد", "middle_name": "محمد", "grandfather_name": "علي", "last_name": "الخطيب",
			"gender": "Male", "program": frappe.db.get_value("Program", {}, "name") or "السابع",
			"academic_year": get_default_academic_year() or "2026-2027", "letter_date": nowdate(),
		}
	)
	return letter_html(doc, design)
