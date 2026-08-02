# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Authentication endpoints for the Match K12 frontend.

Login uses Frappe's own session/cookie mechanism, so the browser keeps an
`sid` cookie and every later call is authenticated automatically. The persona
(admin / teacher / student / parent) is derived from the user's Frappe roles —
never chosen by the client.
"""

import frappe
from frappe import _
from frappe.auth import LoginManager
from frappe.utils import cint

from match_k12.api.utils import (
	fail,
	get_default_academic_term,
	get_default_academic_year,
	get_persona,
	ok,
	resolve_scope,
)


@frappe.whitelist(allow_guest=True)
def login(email: str, password: str):
	"""Authenticate and start a Frappe session."""
	if not email or not password:
		return fail(
			message_en="Email and password are required.",
			message_ar="البريد الإلكتروني وكلمة المرور مطلوبان.",
		)

	try:
		login_manager = LoginManager()
		login_manager.authenticate(user=email, pwd=password)
		login_manager.post_login()
	except frappe.AuthenticationError:
		frappe.local.response["http_status_code"] = 401
		return fail(
			message_en="Invalid email or password.",
			message_ar="البريد الإلكتروني أو كلمة المرور غير صحيحة.",
		)

	persona = get_persona()
	if persona is None:
		# Authenticated but not authorised for this app.
		frappe.local.login_manager.logout()
		frappe.db.commit()
		frappe.local.response["http_status_code"] = 403
		return fail(
			message_en="This account is not linked to a Match K12 role.",
			message_ar="هذا الحساب غير مرتبط بأي دور في نظام Match K12.",
		)

	frappe.local.response["http_status_code"] = 200
	return ok(
		_session_payload(),
		message_en="Signed in successfully.",
		message_ar="تم تسجيل الدخول بنجاح.",
	)


@frappe.whitelist()
def logout():
	frappe.local.login_manager.logout()
	frappe.db.commit()
	return ok(message_en="Signed out.", message_ar="تم تسجيل الخروج.")


@frappe.whitelist(allow_guest=True)
def me():
	"""Return the current session, or success=False when not signed in."""
	if frappe.session.user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return fail(
			message_en="Not signed in.",
			message_ar="لم يتم تسجيل الدخول.",
		)

	persona = get_persona()
	if persona is None:
		frappe.local.response["http_status_code"] = 403
		return fail(
			message_en="This account is not linked to a Match K12 role.",
			message_ar="هذا الحساب غير مرتبط بأي دور في نظام Match K12.",
		)

	return ok(_session_payload())


def _session_payload() -> dict:
	user = frappe.session.user
	persona = get_persona()
	scope = resolve_scope(persona)

	user_doc = frappe.db.get_value(
		"User",
		user,
		["full_name", "user_image", "email", "language"],
		as_dict=True,
	) or {}

	profile = {"display_name": user_doc.get("full_name"), "image": user_doc.get("user_image")}

	# Enrich with the education record this persona is tied to, so the
	# frontend can greet the user properly and scope its own views.
	if scope["student"]:
		student = frappe.db.get_value(
			"Student",
			scope["student"],
			["student_name", "image"],
			as_dict=True,
		)
		if student:
			profile["display_name"] = student.student_name or profile["display_name"]
			profile["image"] = student.image or profile["image"]
	elif scope["instructor"]:
		instructor = frappe.db.get_value(
			"Instructor", scope["instructor"], ["instructor_name", "image"], as_dict=True
		)
		if instructor:
			profile["display_name"] = instructor.instructor_name or profile["display_name"]
			profile["image"] = instructor.image or profile["image"]
	elif scope["guardian"]:
		guardian = frappe.db.get_value(
			"Guardian", scope["guardian"], ["guardian_name", "image"], as_dict=True
		)
		if guardian:
			profile["display_name"] = guardian.guardian_name or profile["display_name"]
			profile["image"] = guardian.image or profile["image"]

	return {
		"user": user,
		"email": user_doc.get("email") or user,
		"role": persona,
		"name": profile["display_name"],
		"image": profile["image"],
		"language": user_doc.get("language") or "ar",
		"scope": {
			"student": scope["student"],
			"instructor": scope["instructor"],
			"guardian": scope["guardian"],
			"students": scope["students"],
		},
		"context": {
			"academic_year": get_default_academic_year(),
			"academic_term": get_default_academic_term(),
		},
	}
