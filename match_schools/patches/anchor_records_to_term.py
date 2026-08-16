"""Anchor every record that happens in a school year to that year and term.

Ten doctypes recorded something that happens inside a term — a submission, a
behaviour note, a quiz attempt, a survey answer — without saying which term it
belonged to. That is invisible in a school's first year and a mess in its
second: last year's records and this year's sit in the same list, every report
that filters by term silently misses them, and "how many detentions this term"
becomes unanswerable.

Existing rows are backfilled from their own date against the Academic Term
calendar, so a school does not lose the history it already has. A row whose
date falls in no configured term keeps the school's current default rather than
being left empty — an approximate anchor beats none, and it is visible and
correctable.

Four doctypes are deliberately left alone: a library book and a bus route are
catalogue entries that outlive any year, a health record is a permanent file,
and a message is dated rather than termly.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# doctype -> the field holding the date that decides its term.
TARGETS = {
	"MS Activity Enrolment": "creation",
	"MS Announcement": "creation",
	"MS Assignment Submission": "creation",
	"MS Behaviour Record": "record_date",
	"MS Book Loan": "issue_date",
	"MS Health Visit": "visit_date",
	"MS Lesson Change": "schedule_date",
	"MS Quiz Attempt": "creation",
	"MS Survey Response": "submitted_on",
	"MS Transport Assignment": "creation",
}


def _term_calendar() -> list[dict]:
	return frappe.get_all(
		"Academic Term",
		fields=["name", "academic_year", "term_start_date", "term_end_date"],
		limit_page_length=0,
	)


def execute():
	fields = {}
	for doctype in TARGETS:
		if not frappe.db.exists("DocType", doctype):
			continue
		fields[doctype] = [
			{
				"fieldname": "ms_academic_year",
				"label": "Academic Year",
				"fieldtype": "Link",
				"options": "Academic Year",
				"insert_after": "naming_series",
			},
			{
				"fieldname": "ms_academic_term",
				"label": "Academic Term",
				"fieldtype": "Link",
				"options": "Academic Term",
				"insert_after": "ms_academic_year",
			},
		]
	if not fields:
		return

	create_custom_fields(fields, ignore_validate=True)

	from match_schools.api.utils import (
		get_default_academic_term,
		get_default_academic_year,
	)

	calendar = _term_calendar()
	fallback_term = get_default_academic_term()
	fallback_year = get_default_academic_year()

	def term_for(when):
		if not when:
			return fallback_year, fallback_term
		day = frappe.utils.getdate(when)
		for t in calendar:
			start, end = t.get("term_start_date"), t.get("term_end_date")
			if start and end and frappe.utils.getdate(start) <= day <= frappe.utils.getdate(end):
				return t.get("academic_year"), t.get("name")
		return fallback_year, fallback_term

	total = 0
	for doctype, date_field in TARGETS.items():
		if doctype not in fields:
			continue
		meta = frappe.get_meta(doctype)
		field = date_field if meta.get_field(date_field) else "creation"
		rows = frappe.get_all(
			doctype,
			filters={"ms_academic_term": ["in", ["", None]]},
			fields=["name", field],
			limit_page_length=0,
		)
		for r in rows:
			year, term = term_for(r.get(field))
			frappe.db.set_value(
				doctype,
				r["name"],
				{"ms_academic_year": year, "ms_academic_term": term},
				update_modified=False,
			)
			total += 1

	frappe.db.commit()
	print(f"Anchored {total} record(s) to a year and term.")
