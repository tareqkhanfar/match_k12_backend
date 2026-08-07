"""Rebuild the naming-series counters for the Match Schools doctypes.

Two things conspired to lose them:

  * the rename moved each counter from `K12-XXX-YYYY-` to `MS-XXX-YYYY-`, and
  * `bench migrate` then deleted the doctype definitions as orphans on a bench
    whose apps.txt lists apps that are not installed, taking the tabSeries rows
    with them.

The result is a counter that restarts at 1 while documents numbered up to
`K12-MSG-2026-00702` already exist, so the next insert collides on the primary
key and Frappe reports that the name already exists.

The fix reads the highest number actually present in each table and seeds the
counter above it, which is correct whatever state the counters were left in.
Both prefixes are seeded: `MS-` because new documents use it, and `K12-`
because the existing rows do, so nothing can collide from either direction.
"""

import re
from collections import defaultdict

import frappe

MODULE = "Match Schools"


def execute():
	fix_property_setters()
	fix_stored_series_values()
	seed_counters()
	frappe.db.commit()


def fix_property_setters():
	"""Rewrite Property Setters that still hand out `K12-` numbers.

	The rename repointed these rows at the new doctype but left their *value*
	alone, and a Property Setter beats the doctype JSON — so a freshly created
	document would still be numbered `K12-MSG-2026-…`, undoing the rebrand for
	everything made from here on.
	"""
	frappe.db.sql(
		"""
		UPDATE `tabProperty Setter`
		   SET value = CONCAT('MS-', SUBSTRING(value, 5))
		 WHERE field_name = 'naming_series'
		   AND property IN ('default', 'options')
		   AND value LIKE 'K12-%'
		"""
	)

	# The row ids embed the old doctype name; harmless, but confusing to read.
	for row in frappe.db.sql(
		"SELECT name FROM `tabProperty Setter` WHERE name LIKE 'K12 %'", as_dict=True
	):
		new = "MS " + row.name[len("K12 ") :]
		if not frappe.db.exists("Property Setter", new):
			frappe.db.sql(
				"UPDATE `tabProperty Setter` SET name = %s WHERE name = %s", (new, row.name)
			)


def fix_stored_series_values():
	"""Point existing documents' `naming_series` field at the new prefix.

	Only the field is rewritten, never the document id: ids are foreign keys in
	other tables. This keeps the form showing the right series when an old
	document is reopened.
	"""
	doctypes = frappe.db.sql(
		"""
		SELECT DISTINCT parent FROM `tabDocField`
		 WHERE fieldname = 'naming_series' AND parent LIKE 'MS %%'
		""",
		as_dict=True,
	)
	for row in doctypes:
		if not frappe.db.table_exists(row.parent):
			continue
		frappe.db.sql(
			"""
			UPDATE `tab{}`
			   SET naming_series = CONCAT('MS-', SUBSTRING(naming_series, 5))
			 WHERE naming_series LIKE 'K12-%%'
			""".format(row.parent)
		)


def highest_used() -> dict[str, int]:
	"""Highest counter per series prefix, taken from the documents themselves."""
	used: dict[str, int] = defaultdict(int)

	doctypes = frappe.db.sql(
		"SELECT name FROM `tabDocType` WHERE module = %s", (MODULE,), as_dict=True
	)
	for row in doctypes:
		table = "tab{}".format(row.name)
		if not frappe.db.table_exists(row.name):
			continue
		for doc in frappe.db.sql("SELECT name FROM `{}`".format(table), as_dict=True):
			match = re.match(r"^(.*?-)(\d+)$", doc.name or "")
			if not match:
				continue
			prefix, number = match.group(1), int(match.group(2))
			if number > used[prefix]:
				used[prefix] = number

	return used


def seed_counters():
	used = highest_used()

	# A K12-prefixed series has an MS-prefixed twin that new documents will
	# use; make sure both start above anything already on disk.
	targets: dict[str, int] = {}
	for prefix, high in used.items():
		targets[prefix] = max(targets.get(prefix, 0), high)
		if prefix.startswith("K12-"):
			twin = "MS-" + prefix[len("K12-") :]
			targets[twin] = max(targets.get(twin, 0), high)

	for prefix, high in sorted(targets.items()):
		current = frappe.db.sql("SELECT current FROM tabSeries WHERE name = %s", prefix)
		if not current:
			frappe.db.sql(
				"INSERT INTO tabSeries (name, current) VALUES (%s, %s)", (prefix, high)
			)
		elif (current[0][0] or 0) < high:
			frappe.db.sql(
				"UPDATE tabSeries SET current = %s WHERE name = %s", (high, prefix)
			)
