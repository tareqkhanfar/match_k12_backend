# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Whitelisted report data for the staff web pages.

The desk report grid is a grid. These endpoints return the same rows already
nested — guardian -> student -> year -> term -> invoice, and student -> program
— so `/guardian-fees-statement` and `/unenrolled-students` can render a
statement a family can be handed rather than a spreadsheet.

Each endpoint imports the desk report's own query and reuses it. That is the
whole point: a filter added to the report reaches the web page for free, and
neither can quietly start reporting a different number from the other.

These return plain nested dicts rather than the `{success, data, message_en,
message_ar}` envelope used by `api/*.py` for the mobile app — they are
consumed by the server-rendered pages in `match_schools/www/`, not by the app.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from match_schools.match_schools.report.guardian_fees_statement import (
	guardian_fees_statement as guardian_report,
)
from match_schools.match_schools.report.unenrolled_students_next_academic_year import (
	unenrolled_students_next_academic_year as unenrolled_report,
)

# The desk reports are staff tools; so are these pages. Anyone who may open the
# report may call the endpoint, and nobody else.
REPORT_ROLES = ("Academics User", "Accounts User", "Accounts Manager", "System Manager")


def _require_staff():
	"""Guard the endpoints the way the report itself is guarded.

	`@frappe.whitelist()` only means "a logged-in user may call this". Without
	this check a parent portal account could read every family's fees by
	calling the endpoint directly, which the desk report would never allow.
	"""
	if frappe.session.user == "Administrator":
		return
	if not set(frappe.get_roles()) & set(REPORT_ROLES):
		frappe.throw(
			_("You are not permitted to view fee reports."), frappe.PermissionError
		)


def _totals():
	return {"paid": 0.0, "outstanding": 0.0, "grand_total": 0.0}


def _add(bucket, row):
	bucket["paid"] += flt(row.get("paid_amount"))
	bucket["outstanding"] += flt(row.get("outstanding_amount"))
	bucket["grand_total"] += flt(row.get("grand_total"))


@frappe.whitelist()
def guardian_fees_statement(
	guardian=None,
	academic_year=None,
	academic_term=None,
	program=None,
	company=None,
	from_date=None,
	to_date=None,
	status="All",
):
	"""One family's fees, nested for the printable statement page.

	Shape::

	    {
	      "guardian":  {"name", "guardian_name"},
	      "currency":  str,
	      "totals":    {"paid", "outstanding", "grand_total"},
	      "students": [
	         {"student", "student_name", "totals": {...},
	          "years": [
	             {"academic_year", "totals": {...},
	              "terms": [
	                 {"academic_term", "totals": {...}, "invoices": [row, ...]}
	              ]}
	          ]}
	      ]
	    }
	"""
	_require_staff()

	if not guardian:
		frappe.throw(_("Please select a Guardian"))

	filters = frappe._dict({
		"guardian": guardian,
		"academic_year": academic_year,
		"academic_term": academic_term,
		"program": program,
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"status": status or "All",
	})

	rows = guardian_report.fetch_rows(filters)
	default_currency = frappe.db.get_default("currency")

	students = {}
	grand = _totals()
	currency = default_currency

	for r in rows:
		currency = r.get("currency") or default_currency
		# Decimal comes back from the driver; the page serialises to JSON.
		for field in ("paid_amount", "outstanding_amount", "grand_total"):
			r[field] = flt(r.get(field))

		student = students.setdefault(r["student"], {
			"student": r["student"],
			"student_name": r["student_name"],
			"totals": _totals(),
			"years": {},
		})
		year = student["years"].setdefault(r["academic_year"] or _("No Academic Year"), {
			"academic_year": r["academic_year"],
			"totals": _totals(),
			"terms": {},
		})
		term = year["terms"].setdefault(r["academic_term"] or _("No Term"), {
			"academic_term": r["academic_term"],
			"totals": _totals(),
			"invoices": [],
		})

		term["invoices"].append(r)
		for bucket in (term["totals"], year["totals"], student["totals"], grand):
			_add(bucket, r)

	# Dicts were only ever the grouping mechanism; the page wants ordered lists.
	students_list = []
	for student in students.values():
		years = []
		for year in student["years"].values():
			year["terms"] = list(year["terms"].values())
			years.append(year)
		student["years"] = years
		students_list.append(student)

	guardian_doc = frappe.db.get_value(
		"Guardian", guardian, ["name", "guardian_name", "mobile_number"], as_dict=True
	) or {"name": guardian, "guardian_name": guardian, "mobile_number": None}

	return {
		"guardian": guardian_doc,
		"currency": currency,
		"totals": grand,
		"students": students_list,
	}


@frappe.whitelist()
def unenrolled_students_next_academic_year(
	from_academic_year=None,
	to_academic_year=None,
	program=None,
	student_batch_name=None,
	student_category=None,
	company=None,
	payment_status="All",
	only_with_outstanding=0,
	exclude_left_students=0,
):
	"""Students who have not re-enrolled, grouped by their previous programme.

	Shape::

	    {
	      "years":    {"from", "to"},
	      "currency": str,
	      "totals":   {"students", "with_outstanding", "left",
	                   "prev_grand_total", "prev_paid",
	                   "prev_outstanding", "total_outstanding"},
	      "programs": [{"program", "totals": {...}, "students": [row, ...]}]
	    }
	"""
	_require_staff()

	filters = frappe._dict({
		"from_academic_year": from_academic_year,
		"to_academic_year": to_academic_year,
		"program": program,
		"student_batch_name": student_batch_name,
		"student_category": student_category,
		"company": company,
		"payment_status": payment_status or "All",
		"only_with_outstanding": cint(only_with_outstanding),
		"exclude_left_students": cint(exclude_left_students),
	})

	# The report appends a total row for the desk grid; the page totals its own
	# groups, so that row would be counted as a student.
	rows = [r for r in unenrolled_report.get_data(filters) if not r.get("is_total")]
	default_currency = frappe.db.get_default("currency")

	programs = {}
	grand = _group_totals()
	currency = default_currency

	for r in rows:
		currency = r.get("currency") or default_currency
		for field in (
			"prev_grand_total", "prev_paid_amount",
			"prev_outstanding_amount", "total_outstanding",
		):
			r[field] = flt(r.get(field))
		# Dates do not survive JSON on their own.
		for field in ("enrollment_date", "date_of_leaving"):
			r[field] = str(r.get(field) or "")

		group = programs.setdefault(r.get("program") or _("No Program"), {
			"program": r.get("program"),
			"totals": _group_totals(),
			"students": [],
		})
		group["students"].append(r)
		_add_group(group["totals"], r)
		_add_group(grand, r)

	return {
		"years": {"from": from_academic_year, "to": to_academic_year},
		"currency": currency,
		"totals": grand,
		"programs": list(programs.values()),
	}


def _group_totals():
	return {
		"students": 0,
		"with_outstanding": 0,
		"left": 0,
		"prev_grand_total": 0.0,
		"prev_paid": 0.0,
		"prev_outstanding": 0.0,
		"total_outstanding": 0.0,
	}


def _add_group(bucket, row):
	bucket["students"] += 1
	bucket["prev_grand_total"] += flt(row.get("prev_grand_total"))
	bucket["prev_paid"] += flt(row.get("prev_paid_amount"))
	bucket["prev_outstanding"] += flt(row.get("prev_outstanding_amount"))
	bucket["total_outstanding"] += flt(row.get("total_outstanding"))
	if flt(row.get("total_outstanding")) > 0:
		bucket["with_outstanding"] += 1
	if row.get("date_of_leaving"):
		bucket["left"] += 1
