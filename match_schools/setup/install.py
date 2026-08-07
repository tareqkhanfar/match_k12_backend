# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe

from match_schools.api.utils import FRAPPE_ROLE_BY_PERSONA


def after_install():
	create_k12_roles()
	patch_fees_income_account_fetch()
	apply_doctype_permissions()
	frappe.db.commit()


def apply_doctype_permissions():
	"""Grant the K12 roles their document permissions.

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


def create_k12_roles():
	"""Create the four K12 personas as Frappe roles (idempotent)."""
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
