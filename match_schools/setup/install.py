# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe

from match_schools.api.utils import FRAPPE_ROLE_BY_PERSONA


def after_install():
	create_persona_roles()
	patch_fees_income_account_fetch()
	install_custom_fields()
	install_patch_fields()
	install_admission_workflow()
	apply_doctype_permissions()
	sync_accounting_workspace()
	frappe.db.commit()


def sync_accounting_workspace():
	"""Add the match_utils accounting tools to Education Accounting, if present.

	Kept out of the shipped workspace JSON on purpose — a Workspace Link to a
	doctype that is not installed fails validation and would break migrate.
	See `setup/accounting_workspace.py`.
	"""
	from match_schools.setup.accounting_workspace import sync

	try:
		sync()
	except Exception:
		# Never let a cosmetic workspace tweak abort an install or a migrate.
		frappe.log_error(
			frappe.get_traceback(), "Education Accounting workspace sync failed"
		)


def install_custom_fields():
	"""Fields this app adds to the Education doctypes (رقم الهوية, username)."""
	from match_schools.setup.custom_fields import create_fields

	create_fields()

	_seed_force_password_change()


# `bench install-app` records every patch in patches.txt as applied without
# running it, so a new site never got the fields these add — every screen that
# read one failed with "Unknown column". Each patch is paired with something it
# creates and runs only while that is missing: several also backfill data, and
# repeating a backfill on every migrate would overwrite what a school set since.
PATCHES_THAT_ADD_FIELDS = (
	("add_academic_period_settings", "Custom Field", "ms_teacher_can_edit_closed_period"),
	("add_gradebook_publishing", "Custom Field", "ms_is_published"),
	("add_grade_appeals_and_release", "Custom Field", "ms_reopened_on"),
	("allow_multiple_behaviour_categories", "Property Setter", ("MS Behaviour Record", "category", "fieldtype")),
	("add_quarter_assessment_tree", "Custom Field", "ms_parent_component"),
	("make_course_schedule_room_optional", "Property Setter", ("Course Schedule", "room", "reqd")),
	("add_timetable_audience", "Custom Field", "ms_audience"),
	("add_lesson_change_original_course", "Custom Field", "original_course"),
	("add_grandfather_name", "Custom Field", "ms_grandfather_name"),
	("add_component_exclusion", "Custom Field", "ms_excluded"),
	("add_component_aggregation", "Custom Field", "ms_aggregation"),
	("add_survey_class_audience", "Custom Field", "ms_student_groups"),
	("add_instructor_user_link", "Custom Field", "ms_user"),
	("add_program_promotion", "Custom Field", "ms_promotion_status"),
	("anchor_records_to_term", "Custom Field", "ms_academic_term"),
	("optional_student_email", "Property Setter", ("Student", "student_email_id", "reqd")),
)


def install_patch_fields():
	import importlib

	for name, kind, marker in PATCHES_THAT_ADD_FIELDS:
		if kind == "Custom Field":
			present = frappe.db.exists("Custom Field", {"fieldname": marker})
		else:
			doc_type, field_name, prop = marker
			present = frappe.db.exists(
				"Property Setter",
				{"doc_type": doc_type, "field_name": field_name, "property": prop},
			)
		if not present:
			importlib.import_module(f"match_schools.patches.{name}").execute()


def _seed_force_password_change():
	"""Turn the first-login password change on, once.

	A Custom Field's `default` only applies to documents created afterwards,
	and Education Settings is a Single that already exists — so without this
	the setting reads 0 and the feature is silently off.

	Seeded exactly once, tracked by its own flag, so a school that switches it
	off does not have it switched back on by the next migrate.
	"""
	if frappe.db.get_default("ms_force_password_change_seeded"):
		return

	frappe.db.set_single_value("Education Settings", "ms_force_password_change", 1)
	frappe.db.set_default("ms_force_password_change_seeded", "1")


def install_admission_workflow():
	"""States and transitions for Student Applicant."""
	from match_schools.setup.admission_workflow import create_workflow

	create_workflow()


def apply_doctype_permissions():
	"""Grant the Match Schools roles their document permissions.

	Without this every write fails with a PermissionError, because Frappe
	checks doctype permissions independently of our API layer.
	"""
	from match_schools.setup.permissions import apply_permissions

	apply_permissions()


def patch_fees_income_account_fetch():
	"""Work around an Education v16 bug that makes Fees uninsertable.

	`Fees.income_account` declares `fetch_from = "fee_structure.income_account"`,
	but Fee Structure has no `income_account` field. Because `fetch_if_empty`
	is 0 the fetch always runs, so saving a Fees record with a fee_structure
	link (which is mandatory) fails with:
		OperationalError: Unknown column 'income_account' in 'SELECT'

	Clearing the fetch_from via a Property Setter keeps the fix inside this
	app instead of editing the education app.
	"""
	meta_field = frappe.db.get_value(
		"DocField", {"parent": "Fees", "fieldname": "income_account"}, "fetch_from"
	)
	if not meta_field:
		return

	fs_fields = frappe.get_meta("Fee Structure").fields
	if any(f.fieldname == "income_account" for f in fs_fields):
		# Upstream fixed it; leave the standard behaviour alone.
		return

	frappe.make_property_setter(
		{
			"doctype": "Fees",
			"fieldname": "income_account",
			"property": "fetch_from",
			"value": "",
			"property_type": "Small Text",
		},
		is_system_generated=True,
	)


def create_persona_roles():
	"""Create the four Match Schools personas as Frappe roles (idempotent)."""
	for role_name in FRAPPE_ROLE_BY_PERSONA.values():
		if frappe.db.exists("Role", role_name):
			continue
		role = frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 0,
				"is_custom": 1,
			}
		)
		role.insert(ignore_permissions=True)
