"""Students transferred out to another school.

A request is written for a pupil (where to, when, why, the ministry
certificate's answers), previewed and printed as the transfer certificate,
and then completed. Completing is the one moment the rest of the system hears
of it, and only as far as the office chose: the pupil is marked as having left,
taken off their sections, their own login switched off, their parents' logins
switched off (never a parent who still has another child here), their bus seat
ended. Everything changed is written down on the request, so a transfer
cancelled afterwards puts each of those back.
"""

import json

import frappe
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from match_schools.api import forms, forms_print, print_designs, transfer_certificate as cert
from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

DOCTYPE = "MS Student Transfer"
STATUS_AR = {"Draft": "مسودة", "Completed": "منفَّذ", "Cancelled": "ملغى"}
STATUS_TONE = {"Draft": "warning", "Completed": "success", "Cancelled": "muted"}

# What completing may do, and what each is on by default. A parent's login is
# off by default: it is shared between children, and switching it off is not
# something to do without being asked.
EFFECTS = {
	"deactivate_student": ("إنهاء قيد الطالب", "يُسجَّل أنه غادر المدرسة بتاريخ النقل، ويختفي من قوائم الطلاب الحاليين.", 1),
	"remove_from_sections": ("إخراجه من الشعب", "يخرج من قوائم شعبه فلا يظهر في الحضور ورصد العلامات.", 1),
	"disable_student_account": ("تعطيل حساب الطالب", "لا يستطيع الدخول إلى النظام أو التطبيق.", 1),
	"disable_guardian_accounts": ("تعطيل حسابات أولياء الأمور", "لا يُعطَّل ولي أمر له ابن آخر ما زال في المدرسة.", 0),
	"end_transport": ("إنهاء اشتراك النقل", "ينتهي اشتراكه في خط الحافلة ويتحرر مقعده.", 1),
}
DEFAULT_EFFECTS = {k: v[2] for k, v in EFFECTS.items()}
CERT_FIELDS = [f[0] for f in cert.FIELDS]


def _protected_user(user: str) -> bool:
	"""Accounts no transfer may switch off: the site's own administrators."""
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _json(value, default):
	try:
		return json.loads(value) if value else default
	except Exception:
		return default


def _name(doc) -> str:
	return doc.student_name or frappe.db.get_value("Student", doc.student, "student_name") or doc.student


def _row(d) -> dict:
	return {
		"id": d.name,
		"student": d.student,
		"studentName": d.student_name,
		"status": d.status,
		"statusLabel": STATUS_AR.get(d.status, d.status),
		"statusTone": STATUS_TONE.get(d.status, "muted"),
		"toSchool": d.to_school,
		"transferDate": str(d.transfer_date or ""),
		"reason": d.reason or "",
		"completedOn": str(d.get("completed_on") or ""),
		"modified": str(d.modified),
	}


def _guardians_of(student: str) -> list[dict]:
	out = []
	for g in frappe.get_all(
		"Student Guardian",
		filters={"parent": student, "parenttype": "Student"},
		fields=["guardian", "guardian_name", "relation"],
		order_by="idx",
	):
		user = frappe.db.get_value("Guardian", g.guardian, "user")
		# Another child still at the school keeps the parent's login alive.
		others = frappe.db.sql(
			"""select count(*) from `tabStudent Guardian` sg join `tabStudent` s on s.name = sg.parent
			   where sg.guardian = %s and sg.parent != %s and s.enabled = 1""",
			(g.guardian, student),
		)[0][0]
		out.append(
			{
				"guardian": g.guardian,
				"name": g.guardian_name,
				"relation": g.relation,
				"user": user,
				"hasOtherChildren": bool(others),
				"enabled": bool(user and cint(frappe.db.get_value("User", user, "enabled"))),
			}
		)
	return out


def _warnings(student: str) -> list[dict]:
	"""What the office should know before a pupil goes: money owed, books out."""
	out = []
	owed = frappe.db.sql(
		"""select coalesce(sum(outstanding_amount), 0) from `tabSales Invoice`
		   where docstatus = 1 and student = %s and outstanding_amount > 0""",
		student,
	)[0][0]
	if flt(owed) > 0:
		out.append({"code": "fees", "text": f"عليه رسوم غير مسددة بقيمة {flt(owed):g}"})
	if frappe.db.table_exists("MS Book Loan"):
		loans = frappe.db.count("MS Book Loan", {"student": student, "status": ["in", ["Issued", "Overdue"]]})
		if loans:
			out.append({"code": "books", "text": f"عنده {loans} كتاب معار لم يُرجَع"})
	return out


def _prefill(student: str) -> dict:
	"""The certificate's answers, as far as the pupil's own record gives them."""
	st = frappe.db.get_value(
		"Student",
		student,
		["student_name", "date_of_birth", "joining_date", "city", "address_line_1", "gender"],
		as_dict=True,
	)
	if not st:
		return {}
	guardian = next(iter(_guardians_of(student)), None)
	occupation = address = ""
	if guardian:
		g = frappe.db.get_value("Guardian", guardian["guardian"], ["occupation", "work_address"], as_dict=True) or {}
		occupation, address = g.get("occupation") or "", g.get("work_address") or ""
	enrolment = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": 1},
		fields=["program", "academic_year"],
		order_by="enrollment_date desc",
		limit=1,
	)
	year = enrolment[0].academic_year if enrolment else get_default_academic_year()
	absent = 0
	start = frappe.db.get_value("Academic Year", year, "year_start_date") if year else None
	if start:
		absent = frappe.db.count(
			"Student Attendance",
			{"student": student, "status": "Absent", "docstatus": 1, "date": [">=", start]},
		)
	last_principal = frappe.db.sql(
		"select certificate from `tabMS Student Transfer` where certificate like '%%\"principal\"%%' order by modified desc limit 1"
	)
	principal = _json(last_principal[0][0], {}).get("principal", "") if last_principal else ""
	return {
		"cert_date": nowdate(),
		"full_name": st.student_name or "",
		"dob": str(st.date_of_birth or ""),
		"guardian": guardian["name"] if guardian else "",
		"guardian_job": occupation,
		"guardian_address": address or st.city or st.address_line_1 or "",
		"religion": "الإسلام",
		"joined": str(st.joining_date or ""),
		"current_grade": enrolment[0].program if enrolment else "",
		"year": year or "",
		"absence": str(absent) if absent else "",
		"principal": principal,
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def options(persona: str = None):
	"""What the new-transfer form needs: the certificate's fields and the effects."""
	return {
		"fields": [
			{
				"fieldname": f[0], "label": f[1], "fieldtype": f[2],
				"options": [o for o in (f[3] or "").split("\n") if o],
				"default": f[4], "width": f[6], "description": f[7],
			}
			for f in cert.FIELDS
		],
		"effects": [
			{"key": k, "label": v[0], "hint": v[1], "default": bool(v[2])} for k, v in EFFECTS.items()
		],
		"statuses": [{"value": k, "label": v, "tone": STATUS_TONE[k]} for k, v in STATUS_AR.items()],
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def prefill(student: str = None, persona: str = None):
	"""The certificate filled from the pupil's record, plus what to watch for."""
	if not student or not frappe.db.exists("Student", student):
		return fail("Student not found.", "لم يتم العثور على الطالب.")
	if frappe.db.exists(DOCTYPE, {"student": student, "status": ["in", ["Draft", "Completed"]]}):
		existing = frappe.db.get_value(
			DOCTYPE, {"student": student, "status": ["in", ["Draft", "Completed"]]}, ["name", "status"], as_dict=True
		)
		return fail(
			"This student already has a transfer request.",
			f"لهذا الطالب طلب نقل قائم ({existing.name} — {STATUS_AR[existing.status]}).",
		)
	return {
		"student": student,
		"studentName": frappe.db.get_value("Student", student, "student_name"),
		"certificate": _prefill(student),
		"guardians": _guardians_of(student),
		"warnings": _warnings(student),
		"enabled": bool(cint(frappe.db.get_value("Student", student, "enabled"))),
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def list_transfers(status: str = None, search: str = None, persona: str = None):
	filters = {"status": status} if status else {}
	or_filters = None
	if search:
		or_filters = [["student_name", "like", f"%{search}%"], ["to_school", "like", f"%{search}%"], ["name", "like", f"%{search}%"]]
	rows = frappe.get_all(
		DOCTYPE,
		filters=filters,
		or_filters=or_filters,
		fields=["name", "student", "student_name", "status", "to_school", "transfer_date", "reason", "completed_on", "modified"],
		order_by="modified desc",
		limit_page_length=500,
	)
	counts = {s: frappe.db.count(DOCTYPE, {"status": s}) for s in STATUS_AR}
	return {"transfers": [_row(r) for r in rows], "counts": counts}


def _detail(doc) -> dict:
	guardians = _guardians_of(doc.student)
	return {
		**_row(doc),
		"certificate": _json(doc.certificate, {}),
		"effects": {**DEFAULT_EFFECTS, **_json(doc.effects, {})},
		"applied": _json(doc.applied, {}),
		"guardians": guardians,
		"warnings": _warnings(doc.student) if doc.status == "Draft" else [],
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def get_transfer(transfer: str = None, persona: str = None):
	if not transfer or not frappe.db.exists(DOCTYPE, transfer):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	return _detail(frappe.get_doc(DOCTYPE, transfer))


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_transfer(payload: str | dict = None, persona: str = None):
	"""Write or amend a request. A completed one is not edited — cancel it."""
	data = parse_json_arg(payload) or {}
	to_school = (data.get("toSchool") or "").strip()
	if not to_school:
		return fail("The new school is required.", "اسم المدرسة المنقول إليها مطلوب.")

	if data.get("id"):
		if not frappe.db.exists(DOCTYPE, data["id"]):
			return fail("Not found.", "لم يتم العثور على الطلب.")
		doc = frappe.get_doc(DOCTYPE, data["id"])
		if doc.status != "Draft":
			return fail("Only a draft can be edited.", "لا يُعدَّل إلا الطلب المسودة — ألغِ المنفَّذ أولًا.")
	else:
		student = data.get("student")
		if not student or not frappe.db.exists("Student", student):
			return fail("Student not found.", "لم يتم العثور على الطالب.")
		if frappe.db.exists(DOCTYPE, {"student": student, "status": ["in", ["Draft", "Completed"]]}):
			return fail("This student already has a transfer request.", "لهذا الطالب طلب نقل قائم.")
		doc = frappe.new_doc(DOCTYPE)
		doc.student = student
		doc.status = "Draft"
		doc.academic_year = get_default_academic_year()
		doc.academic_term = get_default_academic_term()

	doc.student_name = frappe.db.get_value("Student", doc.student, "student_name")
	doc.to_school = to_school
	doc.transfer_date = data.get("transferDate") or doc.transfer_date or nowdate()
	doc.reason = data.get("reason") or ""
	answers = {k: ("" if v is None else str(v)) for k, v in (data.get("certificate") or {}).items() if k in CERT_FIELDS}
	answers["to_school"] = to_school
	doc.certificate = json.dumps(answers, ensure_ascii=False)
	effects = data.get("effects") or {}
	doc.effects = json.dumps({k: 1 if cint(effects.get(k, DEFAULT_EFFECTS[k])) else 0 for k in EFFECTS})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_transfer(transfer: str = None, persona: str = None):
	if not transfer or not frappe.db.exists(DOCTYPE, transfer):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	if frappe.db.get_value(DOCTYPE, transfer, "status") == "Completed":
		return fail("A completed transfer cannot be deleted.", "لا يُحذف طلب منفَّذ — ألغِ تنفيذه أولًا.")
	frappe.delete_doc(DOCTYPE, transfer, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": transfer}


# --- The certificate ------------------------------------------------------------


PRINT_KEY = "student_transfer"


def _form_template():
	"""The certificate's form — its fields: the school's own transfer-certificate
	form if it has one, else the built-in."""
	name = frappe.db.get_value("MS Form Template", {"title": cert.TITLE, "category": "school"}, "name")
	if name:
		return frappe.get_doc("MS Form Template", name)
	doc = frappe.new_doc("MS Form Template")
	doc.title = cert.TITLE
	doc.category = "school"
	doc.entry_for = "Student"
	doc.print_theme = "classic"
	doc.print_orientation = "Portrait"
	doc.print_template = cert.DESIGN
	doc.print_css = cert.CSS
	for fieldname, label, fieldtype, options, default, reqd, width, description in cert.FIELDS:
		doc.append(
			"fields_table",
			{
				"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "options": options,
				"default_value": default, "reqd": reqd, "width": width, "description": description,
			},
		)
	return doc


def _template(design=None):
	"""The certificate's form carrying the design it prints with: the one set
	in Settings › تصاميم الطباعة, else the school's own form's (with the
	letterhead image at its top), else the built-in (print_designs.effective)
	— or `design`, a draft being previewed."""
	return print_designs.apply_to_form(_form_template(), design or print_designs.effective(PRINT_KEY))


def certificate_html(doc, design=None) -> str:
	"""The ministry certificate for this request, with every answer in place."""
	template = _template(design)
	entry = frappe.new_doc("MS Form Entry")
	entry.template = template.name if not template.is_new() else None
	entry.student = doc.student
	entry.student_name = doc.student_name
	entry.filled_on = str(now_datetime())
	answers = {**_json(doc.certificate, {}), "to_school": doc.to_school}
	answers.setdefault("cert_date", str(doc.transfer_date or nowdate()))
	by_name = {f["fieldname"]: f for f in forms._field_rows(template)}
	for fieldname, value in answers.items():
		f = by_name.get(fieldname)
		if f and value not in (None, ""):
			entry.append(
				"values_table",
				{"fieldname": fieldname, "label": f["label"], "fieldtype": f["fieldtype"], "value": str(value)},
			)
	return forms_print.render(template, entry, forms._field_rows(template), False)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def preview(transfer: str = None, persona: str = None):
	"""The certificate as printable HTML — the same page the printer gets."""
	if not transfer or not frappe.db.exists(DOCTYPE, transfer):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	doc = frappe.get_doc(DOCTYPE, transfer)
	try:
		html = certificate_html(doc)
	except Exception as exc:
		frappe.clear_messages()
		return fail("The print design could not be rendered.", forms_print.error_text(exc))
	return {"html": html, "title": f"{cert.TITLE} — {doc.student_name}"}


# --- For the print-design editor ---------------------------------------------------


def print_fields() -> list[dict]:
	"""The certificate's fields, for the editor's list of what a design can name."""
	return forms._field_rows(_form_template())


def print_sample(design) -> str:
	"""The certificate drawn with `design` on the latest request, or on a
	made-up one for a pupil of the school when there is none yet."""
	name = frappe.db.get_value(DOCTYPE, {}, "name", order_by="modified desc")
	if name:
		return certificate_html(frappe.get_doc(DOCTYPE, name), design)
	doc = frappe.new_doc(DOCTYPE)
	student = frappe.db.get_value("Student", {"enabled": 1}, ["name", "student_name"], as_dict=True)
	answers = _prefill(student.name) if student else {"full_name": "أحمد محمد علي الخطيب", "religion": "الإسلام"}
	answers.update({"number": "1/2026", "conduct": "ممتاز", "stream": "الأساسي"})
	doc.update(
		{
			"student": student.name if student else None,
			"student_name": (student.student_name if student else answers["full_name"]),
			"to_school": "مدرسة الأمل الأساسية",
			"transfer_date": nowdate(),
			"certificate": json.dumps(answers, ensure_ascii=False),
		}
	)
	return certificate_html(doc, design)


# --- Completing and undoing --------------------------------------------------------


def _disable_user(user: str | None, log: list, note: str):
	if not user or not frappe.db.exists("User", user):
		return
	if _protected_user(user) or not cint(frappe.db.get_value("User", user, "enabled")):
		return
	frappe.db.set_value("User", user, "enabled", 0)
	# Signed in somewhere right now? Not any more.
	frappe.sessions.clear_sessions(user, force=True)
	log.append({"user": user, "note": note})


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def complete_transfer(transfer: str = None, persona: str = None):
	"""Carry out the request: apply the chosen effects and write down each change."""
	if not transfer or not frappe.db.exists(DOCTYPE, transfer):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	doc = frappe.get_doc(DOCTYPE, transfer)
	if doc.status != "Draft":
		return fail("Already carried out or cancelled.", "هذا الطلب منفَّذ أو ملغى مسبقًا.")
	effects = {**DEFAULT_EFFECTS, **_json(doc.effects, {})}
	sid = doc.student
	date = str(doc.transfer_date or nowdate())
	cert_no = _json(doc.certificate, {}).get("number") or ""
	applied: dict = {"date": date}

	if cint(effects["deactivate_student"]):
		prev = frappe.db.get_value(
			"Student", sid, ["enabled", "date_of_leaving", "leaving_certificate_number", "reason_for_leaving"], as_dict=True
		)
		applied["student"] = dict(prev)
		frappe.db.set_value(
			"Student",
			sid,
			{
				"enabled": 0,
				"date_of_leaving": getdate(date),
				"leaving_certificate_number": cert_no or prev.leaving_certificate_number,
				"reason_for_leaving": (f"نُقل إلى: {doc.to_school}" + (f" — {doc.reason}" if doc.reason else "")),
			},
		)

	if cint(effects["remove_from_sections"]):
		rows = frappe.get_all(
			"Student Group Student",
			filters={"student": sid, "active": 1, "parenttype": "Student Group"},
			fields=["name", "parent"],
			limit_page_length=0,
		)
		for r in rows:
			frappe.db.set_value("Student Group Student", r.name, "active", 0)
		applied["sections"] = [{"row": r.name, "group": r.parent} for r in rows]

	users: list = []
	if cint(effects["disable_student_account"]):
		_disable_user(frappe.db.get_value("Student", sid, "user"), users, "حساب الطالب")
	skipped: list = []
	if cint(effects["disable_guardian_accounts"]):
		for g in _guardians_of(sid):
			if g["hasOtherChildren"]:
				skipped.append({"name": g["name"], "why": "له ابن آخر في المدرسة"})
			else:
				_disable_user(g["user"], users, f"حساب ولي الأمر {g['name']}")
	applied["users"] = users
	applied["guardiansKept"] = skipped

	if cint(effects["end_transport"]) and frappe.db.table_exists("MS Transport Assignment"):
		rows = frappe.get_all(
			"MS Transport Assignment",
			filters={"student": sid, "active": 1},
			fields=["name", "end_date"],
			limit_page_length=0,
		)
		for r in rows:
			frappe.db.set_value("MS Transport Assignment", r.name, {"active": 0, "end_date": getdate(date)})
		applied["transport"] = [{"row": r.name, "end_date": str(r.end_date or "")} for r in rows]

	doc.status = "Completed"
	doc.applied = json.dumps(applied, ensure_ascii=False)
	doc.completed_on = now_datetime()
	doc.completed_by = frappe.session.user
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def cancel_transfer(transfer: str = None, persona: str = None):
	"""Cancel a request. A carried-out one is undone: each change put back."""
	if not transfer or not frappe.db.exists(DOCTYPE, transfer):
		return fail("Not found.", "لم يتم العثور على الطلب.")
	doc = frappe.get_doc(DOCTYPE, transfer)
	if doc.status == "Cancelled":
		return fail("Already cancelled.", "الطلب ملغى مسبقًا.")
	if doc.status == "Completed":
		applied = _json(doc.applied, {})
		if applied.get("student"):
			frappe.db.set_value("Student", doc.student, applied["student"])
		for r in applied.get("sections", []):
			if frappe.db.exists("Student Group Student", r["row"]):
				frappe.db.set_value("Student Group Student", r["row"], "active", 1)
		for u in applied.get("users", []):
			if frappe.db.exists("User", u["user"]):
				frappe.db.set_value("User", u["user"], "enabled", 1)
		for r in applied.get("transport", []):
			if frappe.db.exists("MS Transport Assignment", r["row"]):
				frappe.db.set_value(
					"MS Transport Assignment", r["row"], {"active": 1, "end_date": getdate(r["end_date"]) if r["end_date"] else None}
				)
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _detail(doc)
