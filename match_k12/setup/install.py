# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe

from match_k12.api.utils import FRAPPE_ROLE_BY_PERSONA


def after_install():
	create_k12_roles()
	frappe.db.commit()


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
