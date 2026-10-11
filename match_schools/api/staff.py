"""The office staff's files — secretaries, the vice principal, supervisors
and the nurse.

Teachers, students and parents each had a record; the office had only a
bare User, so the principal could not see who works the front office, keep
their papers, or hand them a password the way every other account gets one.
Everything here is the principal's alone: a secretary does not manage a
colleague's file or login.
"""

import frappe
from frappe.utils import cint

from match_schools.api.credentials import (
	_sync_staff,
	_user_of,
	create_account,
	_link_user,
)
from match_schools.api.utils import (
	FRAPPE_ROLE_BY_PERSONA,
	FRAPPE_ROLE_BY_STAFF_ROLE,
	ROLE_ADMIN,
	ROLE_LABELS_AR,
	ROLE_SECRETARY,
	ROLE_SUPERVISOR,
	apply_period,
	fail,
	ms_endpoint,
	parse_json_arg,
)

STAFF_ROLES = (ROLE_SECRETARY, *FRAPPE_ROLE_BY_STAFF_ROLE)
FRAPPE_STAFF_ROLES = {ROLE_SECRETARY: FRAPPE_ROLE_BY_PERSONA[ROLE_SECRETARY], **FRAPPE_ROLE_BY_STAFF_ROLE}


def _apply_staff_role(user: str | None, role: str):
	"""Give the login exactly this staff role. The principal's own account and
	system managers are left as they are: their role comes from elsewhere."""
	if not user or user in ("Administrator", "Guest") or not frappe.db.exists("User", user):
		return
	held = set(frappe.get_roles(user))
	if "System Manager" in held or FRAPPE_ROLE_BY_PERSONA[ROLE_ADMIN] in held:
		return
	doc = frappe.get_doc("User", user)
	stale = [r for k, r in FRAPPE_STAFF_ROLES.items() if k != role and r in held]
	if stale:
		doc.remove_roles(*stale)
	if FRAPPE_STAFF_ROLES[role] not in held:
		doc.add_roles(FRAPPE_STAFF_ROLES[role])


def _names(doctype: str, field: str, ids: list[str]) -> dict:
	if not ids:
		return {}
	return dict(frappe.get_all(doctype, filters={"name": ["in", ids]}, fields=["name", field], as_list=True))

DOCTYPE = "MS Staff Member"
EDITABLE = (
	"full_name", "job_title", "status", "gender", "date_of_birth", "national_id",
	"phone", "email", "joining_date", "address", "qualification", "notes", "image",
)


def _row(d) -> dict:
	user = d.get("user")
	account = None
	if user and frappe.db.exists("User", user):
		u = frappe.db.get_value("User", user, ["username", "enabled", "last_login"], as_dict=True)
		account = {
			"user": user,
			"username": u.username or user.split("@")[0],
			"enabled": bool(cint(u.enabled)),
			"lastLogin": str(u.last_login or ""),
		}
	role = d.get("staff_role") or ROLE_SECRETARY
	programs = parse_json_arg(d.get("supervised_programs"), []) or []
	groups = parse_json_arg(d.get("supervised_groups"), []) or []
	group_names = _names("Student Group", "student_group_name", groups)
	return {
		"id": d.name,
		"fullName": d.full_name,
		"role": role,
		"roleLabel": ROLE_LABELS_AR.get(role, role),
		"supervisedPrograms": programs,
		"supervisedGroups": [{"id": g, "label": group_names.get(g) or g} for g in groups],
		"jobTitle": d.job_title or "",
		"status": d.status or "Active",
		"gender": d.gender or "",
		"dateOfBirth": str(d.date_of_birth or ""),
		"nationalId": d.national_id or "",
		"phone": d.phone or "",
		"email": d.email or "",
		"joiningDate": str(d.joining_date or ""),
		"address": d.address or "",
		"qualification": d.qualification or "",
		"notes": d.notes or "",
		"image": d.image or "",
		"account": account,
		"modified": str(d.modified),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def list_staff(search: str = None, persona: str = None):
	_sync_staff()
	frappe.db.commit()
	filters = {}
	or_filters = None
	if search:
		or_filters = [["full_name", "like", f"%{search}%"], ["phone", "like", f"%{search}%"]]
	rows = frappe.get_all(
		DOCTYPE, filters=filters, or_filters=or_filters, fields=["*"], order_by="full_name asc",
		limit_page_length=500,
	)
	return {"staff": [_row(r) for r in rows]}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def get_staff(name: str = None, persona: str = None):
	if not name or not frappe.db.exists(DOCTYPE, name):
		return fail("Not found.", "لم يتم العثور على الملف.")
	return {"staff": _row(frappe.get_doc(DOCTYPE, name))}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_staff(payload: str | dict = None, persona: str = None):
	"""Create or update a file. A new file can get its login at once; the
	password comes back this one time."""
	data = parse_json_arg(payload) or {}
	if not (data.get("fullName") or "").strip():
		return fail("A name is required.", "الاسم الكامل مطلوب.")
	doc = frappe.get_doc(DOCTYPE, data["id"]) if data.get("id") else frappe.new_doc(DOCTYPE)
	camel = {
		"full_name": "fullName", "job_title": "jobTitle", "date_of_birth": "dateOfBirth",
		"national_id": "nationalId", "joining_date": "joiningDate",
	}
	for field in EDITABLE:
		key = camel.get(field, field)
		if key in data:
			doc.set(field, (data.get(key) or None) if field in ("date_of_birth", "joining_date") else data.get(key))
	doc.full_name = doc.full_name.strip()
	if "role" in data:
		role = data.get("role") or ROLE_SECRETARY
		if role not in STAFF_ROLES:
			return fail("Unknown role.", "دور غير معروف.")
		doc.staff_role = role
	role = doc.staff_role or ROLE_SECRETARY
	if role == ROLE_SUPERVISOR:
		if "supervisedPrograms" in data:
			doc.supervised_programs = frappe.as_json(
				[p for p in (data.get("supervisedPrograms") or []) if frappe.db.exists("Program", p)]
			)
		if "supervisedGroups" in data:
			doc.supervised_groups = frappe.as_json(
				[g for g in (data.get("supervisedGroups") or []) if frappe.db.exists("Student Group", g)]
			)
	else:
		doc.supervised_programs = None
		doc.supervised_groups = None
	doc.save(ignore_permissions=True)
	_apply_staff_role(doc.user, role)

	credentials = None
	if cint(data.get("createAccount")) and not _user_of(DOCTYPE, doc.name):
		credentials = create_account(role, doc.name, doc.full_name, mobile=doc.phone)
		_link_user(DOCTYPE, doc.name, credentials["user"])
		doc.reload()
	frappe.db.commit()
	return {"staff": _row(doc), "credentials": credentials, "message_ar": "تم الحفظ."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def delete_staff(name: str = None, persona: str = None):
	"""Remove a file; its login is switched off rather than deleted, so what
	the person did in the system stays attributed to them."""
	if not name or not frappe.db.exists(DOCTYPE, name):
		return fail("Not found.", "لم يتم العثور على الملف.")
	user = frappe.db.get_value(DOCTYPE, name, "user")
	if user and user != "Administrator" and "System Manager" not in frappe.get_roles(user):
		frappe.db.set_value("User", user, "enabled", 0)
	frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {"message_ar": "تم حذف الملف وتعطيل حساب الدخول."}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def staff_options(persona: str = None):
	"""The roles a staff file can hold, and the grades and sections a
	supervisor can be given."""
	group_filters: dict = {"disabled": 0}
	apply_period(group_filters, "Student Group")
	return {
		"roles": [{"key": r, "label": ROLE_LABELS_AR.get(r, r)} for r in STAFF_ROLES],
		"programs": [
			{"id": p.name, "label": p.program_name or p.name}
			for p in frappe.get_all("Program", fields=["name", "program_name"], order_by="name asc")
		],
		"groups": [
			{"id": g.name, "label": g.student_group_name or g.name, "program": g.program or ""}
			for g in frappe.get_all(
				"Student Group",
				filters=group_filters,
				fields=["name", "student_group_name", "program"],
				order_by="program asc, student_group_name asc",
				limit_page_length=0,
			)
		],
	}
