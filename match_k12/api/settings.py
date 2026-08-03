# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""School settings: academic year/term, school details and role counts."""

import frappe
import frappe.defaults
from frappe import _

from match_k12.api.utils import (
	FRAPPE_ROLE_BY_PERSONA,
	PERSONA_LABELS_AR,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	k12_endpoint,
	resolve_scope,
)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN)
def get_settings(persona: str = None):
	"""Everything the settings screen shows."""
	company_name = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	company = (
		frappe.db.get_value(
			"Company",
			company_name,
			["name", "company_name", "email", "phone_no", "country", "default_currency"],
			as_dict=True,
		)
		if company_name
		else {}
	) or {}

	return {
		"school": {
			"company": company.get("name"),
			"name": company.get("company_name"),
			"email": company.get("email"),
			"phone": company.get("phone_no"),
			"country": company.get("country"),
			"currency": company.get("default_currency"),
		},
		"academic": {
			"current_year": frappe.db.get_single_value(
				"Education Settings", "current_academic_year"
			),
			"current_term": frappe.db.get_single_value(
				"Education Settings", "current_academic_term"
			),
			"years": frappe.get_all(
				"Academic Year",
				fields=["name", "year_start_date", "year_end_date"],
				order_by="year_start_date desc",
			),
			"terms": frappe.get_all(
				"Academic Term",
				fields=["name", "academic_year", "term_start_date", "term_end_date"],
				order_by="term_start_date desc",
			),
		},
		"roles": _role_counts(),
		"policies": {
			# Whether a student may write outside their own teachers.
			"student_open_messaging": _student_open_messaging(),
		},
		"counts": {
			"students": frappe.db.count("Student", {"enabled": 1}),
			"instructors": frappe.db.count("Instructor"),
			"guardians": frappe.db.count("Guardian"),
			"programs": frappe.db.count("Program"),
			"courses": frappe.db.count("Course"),
			"student_groups": frappe.db.count("Student Group", {"disabled": 0}),
		},
	}


def _student_open_messaging() -> bool:
	from match_k12.api.messaging import student_open_messaging

	return student_open_messaging()


def _role_counts() -> list[dict]:
	"""How many users hold each K12 persona role."""
	out = []
	labels = {
		"admin": "مدير المدرسة",
		"teacher": "معلم",
		"student": "طالب",
		"parent": "ولي أمر",
	}
	for persona, role in FRAPPE_ROLE_BY_PERSONA.items():
		count = frappe.db.count("Has Role", {"role": role, "parenttype": "User"})
		out.append(
			{"persona": persona, "role": role, "label": labels.get(persona, persona), "users": count}
		)
	return out


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN)
def save_settings(payload: str | dict, persona: str = None):
	"""Update the school details and the active academic year/term."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	updated = []

	# --- Academic year / term ---
	year = data.get("current_year")
	if year:
		if not frappe.db.exists("Academic Year", year):
			return fail(
				message_en=f"Academic Year {year} not found.",
				message_ar="السنة الدراسية غير موجودة.",
			)
		frappe.db.set_single_value("Education Settings", "current_academic_year", year)
		updated.append("current_year")

	term = data.get("current_term")
	if term:
		if not frappe.db.exists("Academic Term", term):
			return fail(
				message_en=f"Academic Term {term} not found.",
				message_ar="الفصل الدراسي غير موجود.",
			)
		frappe.db.set_single_value("Education Settings", "current_academic_term", term)
		updated.append("current_term")

	# --- School (Company) details ---
	company = data.get("company")
	if company and frappe.db.exists("Company", company):
		fields = {
			"company_name": data.get("name"),
			"email": data.get("email"),
			"phone_no": data.get("phone"),
		}
		fields = {k: v for k, v in fields.items() if v is not None}
		if fields:
			doc = frappe.get_doc("Company", company)
			doc.update(fields)
			doc.save()
			updated.extend(fields.keys())

	frappe.db.commit()
	return {
		"success": True,
		"data": {"updated": updated},
		"message_en": "Settings saved.",
		"message_ar": "تم حفظ الإعدادات.",
	}


# --- Personal profile and preferences --------------------------------------
# Every persona has these; the school-wide settings above stay admin-only.

PREFERENCE_KEYS = {
	"k12_pref_language": "language",
	"k12_pref_theme": "theme",
	"k12_pref_notify_email": "notify_email",
	"k12_pref_notify_announcements": "notify_announcements",
	"k12_pref_notify_grades": "notify_grades",
	"k12_pref_notify_attendance": "notify_attendance",
	"k12_pref_density": "density",
}

PREFERENCE_DEFAULTS = {
	"language": "ar",
	"theme": "system",
	"notify_email": "1",
	"notify_announcements": "1",
	"notify_grades": "1",
	"notify_attendance": "1",
	"density": "comfortable",
}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def my_profile(persona: str = None):
	"""The signed-in user's own record, plus whatever their role links to."""
	user = frappe.get_doc("User", frappe.session.user)
	scope = resolve_scope(persona)

	profile = {
		"user": user.name,
		"email": user.email,
		"full_name": user.full_name,
		"first_name": user.first_name,
		"last_name": user.last_name,
		"phone": user.phone or user.mobile_no,
		"image": user.user_image,
		"persona": persona,
		"persona_label": PERSONA_LABELS_AR.get(persona, persona),
		"last_login": str(user.last_login or ""),
		"linked": None,
	}

	# The record behind the login, so the profile is not just an account page.
	if persona == ROLE_STUDENT and scope.get("student"):
		s = frappe.db.get_value(
			"Student",
			scope["student"],
			["name", "student_name", "student_email_id", "student_mobile_number",
			 "date_of_birth", "gender", "blood_group", "image"],
			as_dict=True,
		)
		if s:
			enrollment = frappe.get_all(
				"Program Enrollment",
				filters={"student": s.name, "docstatus": ["<", 2]},
				fields=["program", "academic_year", "student_batch_name"],
				order_by="creation desc",
				limit=1,
			)
			profile["linked"] = {
				"type": "student",
				"id": s.name,
				"name": s.student_name,
				"email": s.student_email_id,
				"phone": s.student_mobile_number,
				"date_of_birth": str(s.date_of_birth or ""),
				"gender": s.gender,
				"blood_group": s.blood_group,
				"program": enrollment[0].program if enrollment else None,
				"batch": enrollment[0].student_batch_name if enrollment else None,
				"academic_year": enrollment[0].academic_year if enrollment else None,
			}
	elif persona == ROLE_TEACHER and scope.get("instructor"):
		i = frappe.db.get_value(
			"Instructor", scope["instructor"], ["name", "instructor_name", "department"], as_dict=True
		)
		if i:
			groups = frappe.get_all(
				"Student Group Instructor",
				filters={"instructor": i.name, "parenttype": "Student Group"},
				pluck="parent",
			)
			profile["linked"] = {
				"type": "instructor",
				"id": i.name,
				"name": i.instructor_name,
				"department": i.department,
				"groups": len(set(groups)),
			}
	elif persona == ROLE_PARENT and scope.get("guardian"):
		g = frappe.db.get_value(
			"Guardian",
			scope["guardian"],
			["name", "guardian_name", "email_address", "mobile_number", "occupation"],
			as_dict=True,
		)
		if g:
			profile["linked"] = {
				"type": "guardian",
				"id": g.name,
				"name": g.guardian_name,
				"email": g.email_address,
				"phone": g.mobile_number,
				"occupation": g.occupation,
				"children": len(scope.get("students") or []),
			}

	return {"profile": profile, "preferences": _get_preferences()}


def _get_preferences() -> dict:
	"""Per-user preferences, falling back to the defaults."""
	stored = frappe.defaults.get_defaults(frappe.session.user) or {}
	out = dict(PREFERENCE_DEFAULTS)
	for key, name in PREFERENCE_KEYS.items():
		if stored.get(key) is not None:
			out[name] = str(stored[key])
	return out


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def save_my_profile(payload: str | dict, persona: str = None):
	"""Update the parts of their own account a user is allowed to change."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	user = frappe.get_doc("User", frappe.session.user)
	# Deliberately narrow: a user may not change their own email or roles here.
	for field in ("first_name", "last_name", "phone", "mobile_no"):
		if data.get(field) is not None:
			setattr(user, field, data[field])
	if data.get("image") is not None:
		user.user_image = data["image"]
	user.save(ignore_permissions=True)

	prefs = data.get("preferences") or {}
	by_name = {name: key for key, name in PREFERENCE_KEYS.items()}
	for name, value in prefs.items():
		key = by_name.get(name)
		if key:
			frappe.defaults.set_user_default(key, str(value))

	frappe.db.commit()
	return {
		"success": True,
		"data": {"user": user.name, "preferences": _get_preferences()},
		"message_en": "Profile updated.",
		"message_ar": "تم تحديث الملف الشخصي.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def change_password(current_password: str, new_password: str, persona: str = None):
	"""Change the signed-in user's password, verifying the current one first."""
	from frappe.utils.password import check_password, update_password

	if not new_password or len(new_password) < 8:
		return fail(
			message_en="The new password must be at least 8 characters.",
			message_ar="كلمة المرور الجديدة يجب أن تكون ٨ أحرف على الأقل.",
		)

	try:
		check_password(frappe.session.user, current_password)
	except frappe.AuthenticationError:
		return fail(
			message_en="The current password is incorrect.",
			message_ar="كلمة المرور الحالية غير صحيحة.",
		)

	update_password(frappe.session.user, new_password)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"user": frappe.session.user},
		"message_en": "Password changed.",
		"message_ar": "تم تغيير كلمة المرور.",
	}
