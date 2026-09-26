"""The office staff's files — the secretaries.

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
	ROLE_ADMIN,
	ROLE_SECRETARY,
	fail,
	ms_endpoint,
	parse_json_arg,
)

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
	return {
		"id": d.name,
		"fullName": d.full_name,
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
	doc.save(ignore_permissions=True)

	credentials = None
	if cint(data.get("createAccount")) and not _user_of(DOCTYPE, doc.name):
		credentials = create_account(ROLE_SECRETARY, doc.name, doc.full_name, mobile=doc.phone)
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
