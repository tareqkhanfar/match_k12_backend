"""File every existing record into the term it actually happened in.

The period fields were added after these rows were written, so they are all
blank — which means the year switcher hides every one of them. Each row is
filed by its own date against the term calendar rather than by today's term:
a behaviour note from the first term belongs there, not wherever the school
happens to be standing when this runs.

Written as SQL rather than through the ORM: 900-odd documents saved one by one
would each fire validation and version tracking for a two-column fill, and
several of these doctypes are submittable, where a normal save is refused.
"""

import frappe

from match_schools.academic_stamp import DATE_SOURCE, INHERIT


def execute():
	terms = frappe.get_all(
		"Academic Term",
		fields=["name", "academic_year", "term_start_date", "term_end_date"],
		order_by="term_start_date",
	)
	years = frappe.get_all(
		"Academic Year", fields=["name", "year_start_date", "year_end_date"]
	)
	if not terms and not years:
		return

	# Children first is wrong: a child reads its parent's stamp, so parents
	# have to be filled before the rows that inherit from them.
	for doctype in list(DATE_SOURCE) + list(INHERIT):
		if not frappe.db.table_exists(doctype):
			continue
		meta = frappe.get_meta(doctype)
		if not meta.has_field("academic_year"):
			continue
		_fill(doctype, terms, years, meta)

	# Whatever the calendar could not place: a standing record with no date of
	# its own, or a row whose date was never filled in. The school's current
	# period is the only defensible guess, and leaving them blank is the one
	# outcome that is certainly wrong — a blank row is invisible everywhere.
	for doctype in set(list(DATE_SOURCE) + list(INHERIT) + ["MS Health Record", "MS Grade Rule"]):
		if frappe.db.table_exists(doctype) and frappe.get_meta(doctype).has_field("academic_year"):
			_fill_default(doctype)

	frappe.db.commit()


def _fill(doctype: str, terms: list, years: list, meta) -> None:
	table = f"tab{doctype}"
	field = DATE_SOURCE.get(doctype)

	if doctype in INHERIT:
		link, parent_dt = INHERIT[doctype]
		if meta.has_field(link) and frappe.db.table_exists(parent_dt):
			frappe.db.sql(
				f"""
				update `{table}` child
				  join `tab{parent_dt}` parent on parent.name = child.`{link}`
				   set child.academic_year = parent.academic_year,
				       child.academic_term = parent.academic_term
				 where ifnull(child.academic_year, '') = ''
				   and ifnull(parent.academic_year, '') != ''
				"""
			)
		if not field:
			return

	if not field or not meta.has_field(field):
		return

	for term in terms:
		frappe.db.sql(
			f"""
			update `{table}`
			   set academic_year = %(year)s, academic_term = %(term)s
			 where ifnull(academic_year, '') = ''
			   and date(`{field}`) between %(start)s and %(end)s
			""",
			{
				"year": term.academic_year,
				"term": term.name,
				"start": term.term_start_date,
				"end": term.term_end_date,
			},
		)

	# Dates that fall in a year but between its terms — a summer loan, a note
	# written in the holiday. The year is still known and worth keeping.
	for year in years:
		frappe.db.sql(
			f"""
			update `{table}`
			   set academic_year = %(year)s
			 where ifnull(academic_year, '') = ''
			   and date(`{field}`) between %(start)s and %(end)s
			""",
			{"year": year.name, "start": year.year_start_date, "end": year.year_end_date},
		)


def _fill_default(doctype: str) -> None:
	from match_schools.api.utils import get_default_academic_term, get_default_academic_year

	year = get_default_academic_year()
	if not year:
		return
	frappe.db.sql(
		f"""
		update `tab{doctype}`
		   set academic_year = %(year)s, academic_term = %(term)s
		 where ifnull(academic_year, '') = ''
		""",
		{"year": year, "term": get_default_academic_term()},
	)
