"""Rename the K12-era metadata to the Match Schools branding.

This runs *before* model sync so that the existing doctypes are renamed in
place — tables and all — rather than the sync creating a fresh set of `MS *`
doctypes and stranding the old `tabK12 *` tables with every row still in them.

Everything here is guarded so a second run is a no-op, and so a partially
applied run (interrupted migrate) picks up where it left off.

Document *names* are deliberately left alone: rows created under the old
`K12-ASG-2026-00001` series keep those ids, because they are foreign keys in
other tables. Only the series prefix for *future* documents changes, which the
renamed doctype JSON already carries.
"""

import frappe

MODULE_OLD = "Match K12"
MODULE_NEW = "Match Schools"

ROLES = {
	"K12 School Admin": "MS School Admin",
	"K12 Secretary": "MS Secretary",
	"K12 Teacher": "MS Teacher",
	"K12 Student": "MS Student",
	"K12 Parent": "MS Parent",
}


def execute():
	rename_module()
	rename_doctypes()
	rename_roles()
	repoint_references()
	frappe.db.commit()


def rename_module():
	"""Point the Module Def at the new app.

	`frappe.rename_doc` refuses non-custom modules, so the row is inserted and
	the old one dropped directly — a Module Def carries no child tables, and
	the doctypes that reference it are repointed just below.
	"""
	if not frappe.db.exists("Module Def", MODULE_NEW):
		doc = frappe.new_doc("Module Def")
		doc.module_name = MODULE_NEW
		doc.app_name = "match_schools"
		doc.custom = 0
		doc.insert(ignore_permissions=True)
	else:
		frappe.db.set_value(
			"Module Def", MODULE_NEW, "app_name", "match_schools", update_modified=False
		)

	# Repoint before deleting, so nothing is left dangling.
	frappe.db.sql(
		"UPDATE `tabDocType` SET module = %s WHERE module = %s", (MODULE_NEW, MODULE_OLD)
	)
	frappe.db.delete("Module Def", {"module_name": MODULE_OLD})


def rename_doctypes():
	"""K12 X -> MS X, which also renames `tabK12 X` to `tabMS X`."""
	rows = frappe.db.sql(
		"SELECT name FROM `tabDocType` WHERE name LIKE 'K12 %%'", as_dict=True
	)
	for row in rows:
		old = row.name
		new = "MS " + old[len("K12 ") :]

		if frappe.db.exists("DocType", new):
			# A previous partial run already made it; drop the leftover.
			continue

		frappe.rename_doc("DocType", old, new, force=True, show_alert=False)
		frappe.db.set_value("DocType", new, "module", MODULE_NEW, update_modified=False)


def rename_roles():
	for old, new in ROLES.items():
		if frappe.db.exists("Role", old) and not frappe.db.exists("Role", new):
			frappe.rename_doc("Role", old, new, force=True, show_alert=False)


def repoint_references():
	"""Fix the columns that store a doctype or role name as plain text.

	`frappe.rename_doc` updates true Link fields, but these carry the names in
	free-form columns it does not know to follow.
	"""
	pairs = [
		("`tabDocField`", "options"),
		("`tabCustom Field`", "options"),
		("`tabCustom Field`", "dt"),
		("`tabProperty Setter`", "doc_type"),
		("`tabCustom DocPerm`", "parent"),
		("`tabDocPerm`", "parent"),
	]
	for table, column in pairs:
		frappe.db.sql(
			f"UPDATE {table} SET {column} = CONCAT('MS ', SUBSTRING({column}, 5)) "
			f"WHERE {column} LIKE 'K12 %%'"
		)

	# Role names held in permission and assignment rows.
	for table, column in [
		("`tabCustom DocPerm`", "role"),
		("`tabDocPerm`", "role"),
		("`tabHas Role`", "role"),
	]:
		for old, new in ROLES.items():
			frappe.db.sql(
				f"UPDATE {table} SET {column} = %s WHERE {column} = %s", (new, old)
			)

	# Naming series counters, so numbering continues rather than restarting.
	for row in frappe.db.sql("SELECT name FROM tabSeries WHERE name LIKE 'K12-%%'", as_dict=True):
		new = "MS-" + row.name[len("K12-") :]
		exists = frappe.db.sql("SELECT current FROM tabSeries WHERE name = %s", new)
		if exists:
			frappe.db.sql(
				"UPDATE tabSeries SET current = GREATEST(current, "
				"(SELECT current FROM (SELECT current FROM tabSeries WHERE name = %s) t)) "
				"WHERE name = %s",
				(row.name, new),
			)
			frappe.db.sql("DELETE FROM tabSeries WHERE name = %s", row.name)
		else:
			frappe.db.sql("UPDATE tabSeries SET name = %s WHERE name = %s", (new, row.name))
