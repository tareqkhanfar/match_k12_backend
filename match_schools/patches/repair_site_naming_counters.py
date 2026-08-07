"""Reseed naming-series counters that have fallen behind the documents.

Not limited to this app: the same `bench migrate` that dropped the Match
Schools counters also dropped counters belonging to Education and other
modules, so a school creating a new Fee or Course Enrollment hits "name already
exists" exactly as it did for the renamed doctypes.

Two naming mechanisms both draw from `tabSeries` and both need covering:

    naming_series:                    Fees, Fee Structure, Course Schedule
    EDU-RES-.YYYY.-.#####  /  format: Assessment Result, Course Enrollment, Room

Only counters that are missing or lower than the data are touched; a counter
that is already ahead is left alone, because deliberate gaps are legitimate.
Document names are never modified.
"""

import re
from collections import defaultdict

import frappe


def execute():
	repair()
	frappe.db.commit()


def _expand(option: str) -> str | None:
	"""`EDU-FEE-.YYYY.-` -> `EDU-FEE-2026-`; None if it needs runtime context."""
	if not option:
		return None
	today = frappe.utils.nowdate()
	s = option.strip()
	# The counter placeholder is not part of the tabSeries key.
	s = re.sub(r"\.?#+$", "", s)
	s = s.replace(".YYYY.", today[:4]).replace(".YY.", today[2:4])
	s = s.replace(".MM.", today[5:7]).replace(".DD.", today[8:10])
	s = s.replace("{YYYY}", today[:4]).replace("{YY}", today[2:4])
	s = s.replace("{MM}", today[5:7]).replace("{DD}", today[8:10])
	s = re.sub(r"\{#+\}$", "", s)
	if "." in s or "{" in s:
		# Still holds a field reference (.company., {series}); can't resolve it
		# without a document, and those are per-record anyway.
		return None
	return s or None


def declared_prefixes() -> dict[str, str]:
	"""Every series prefix this site is configured to hand out."""
	found: dict[str, str] = {}

	# 1. naming_series field options, including any Property Setter override.
	rows = frappe.db.sql(
		"""
		SELECT parent AS doctype, options FROM `tabDocField`
		 WHERE fieldname = 'naming_series' AND IFNULL(options, '') != ''
		UNION ALL
		SELECT doc_type AS doctype, value AS options FROM `tabProperty Setter`
		 WHERE field_name = 'naming_series' AND property = 'options'
		   AND IFNULL(value, '') != ''
		""",
		as_dict=True,
	)
	for r in rows:
		for opt in str(r.options).split("\n"):
			p = _expand(opt)
			if p:
				found.setdefault(p, r.doctype)

	# 2. autoname patterns: `EDU-RES-.YYYY.-.#####` and `format:MRL-{YYYY}-{#####}`.
	for r in frappe.db.sql(
		"SELECT name, autoname FROM `tabDocType` WHERE IFNULL(autoname, '') != ''",
		as_dict=True,
	):
		auto = r.autoname
		if auto.startswith("naming_series:"):
			continue
		if auto.startswith("format:"):
			auto = auto[len("format:") :]
		elif ":" in auto.split("-")[0]:
			continue  # field:, hash, prompt, autoincrement …
		p = _expand(auto)
		if p:
			found.setdefault(p, r.name)

	return found


def repair():
	declared = declared_prefixes()
	if not declared:
		return

	used: dict[str, int] = defaultdict(int)

	doctypes = frappe.db.sql(
		"SELECT name FROM `tabDocType` WHERE issingle = 0 AND istable = 0", as_dict=True
	)
	for row in doctypes:
		if not frappe.db.table_exists(row.name):
			continue
		try:
			names = frappe.db.sql("SELECT name FROM `tab{}`".format(row.name), as_dict=True)
		except Exception:
			continue
		for doc in names:
			# An exact "<prefix><digits>" match. Amended documents carry a
			# trailing `-1` and so are skipped, which is what we want: they do
			# not consume a counter value.
			m = re.match(r"^(.*?-)(\d+)$", doc.name or "")
			if not m:
				continue
			prefix, number = m.group(1), int(m.group(2))
			if prefix in declared and number > used[prefix]:
				used[prefix] = number

	for prefix, high in sorted(used.items()):
		current = frappe.db.sql("SELECT current FROM tabSeries WHERE name = %s", prefix)
		if not current:
			frappe.db.sql(
				"INSERT INTO tabSeries (name, current) VALUES (%s, %s)", (prefix, high)
			)
			frappe.logger().info("naming series {} seeded at {}".format(prefix, high))
		elif (current[0][0] or 0) < high:
			frappe.db.sql(
				"UPDATE tabSeries SET current = %s WHERE name = %s", (high, prefix)
			)
			frappe.logger().info(
				"naming series {} raised {} -> {}".format(prefix, current[0][0], high)
			)
