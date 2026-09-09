# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe

from match_schools.api.utils import FRAPPE_ROLE_BY_PERSONA


def after_install():
	create_persona_roles()
	patch_fees_income_account_fetch()
	install_custom_fields()
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
