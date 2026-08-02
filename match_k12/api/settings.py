# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""School settings: academic year/term, school details and role counts."""

import frappe
from frappe import _

from match_k12.api.utils import (
	FRAPPE_ROLE_BY_PERSONA,
	ROLE_ADMIN,
	fail,
	k12_endpoint,
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
		"counts": {
			"students": frappe.db.count("Student", {"enabled": 1}),
			"instructors": frappe.db.count("Instructor"),
			"guardians": frappe.db.count("Guardian"),
			"programs": frappe.db.count("Program"),
			"courses": frappe.db.count("Course"),
			"student_groups": frappe.db.count("Student Group", {"disabled": 0}),
		},
	}


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
