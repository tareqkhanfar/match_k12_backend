# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""One family's whole fee position, nested Student -> Year -> Term -> Invoice.

A guardian with three children across two years asks a single question at the
counter — "what do I owe?" — and this answers it in one screen, with a subtotal
at every level so the conversation can happen at whatever granularity the
family wants.

Reads Sales Invoice, the v16 fee model.
"""

import frappe
from frappe import _

from match_schools import ms_fee_reporting as fr

FILTER_KEYS = ("company", "program", "academic_year", "academic_term")


def execute(filters=None):
	filters = frappe._dict(filters or {})

	data = get_data(filters)
	details = detail_rows(data)

	return (
		get_columns(),
		data,
		None,
		fr.paid_outstanding_chart(details, "paid_amount", "outstanding_amount"),
		fr.money_summary(details, "grand_total", "paid_amount", "outstanding_amount"),
	)


def detail_rows(data):
	"""Invoice rows only — the headers and subtotals would double every total."""
	return [d for d in data if not d.get("is_group") and not d.get("is_subtotal")]


def get_columns():
	return [
		{"label": _("Student / Year / Term"), "fieldname": "student", "fieldtype": "Link",
		 "options": "Student", "width": 220},
		{"label": _("Student Name"), "fieldname": "student_name", "fieldtype": "Data",
		 "width": 220},
		{"label": _("Academic Year"), "fieldname": "academic_year", "fieldtype": "Link",
		 "options": "Academic Year", "width": 130},
		{"label": _("Academic Term"), "fieldname": "academic_term", "fieldtype": "Link",
		 "options": "Academic Term", "width": 140},
		{"label": _("Program"), "fieldname": "program", "fieldtype": "Link",
		 "options": "Program", "width": 130},
		{"label": _("Batch"), "fieldname": "student_batch_name", "fieldtype": "Link",
		 "options": "Student Batch Name", "width": 110},
		{"label": _("Category"), "fieldname": "student_category", "fieldtype": "Link",
		 "options": "Student Category", "width": 120},
		{"label": _("Fee Schedule"), "fieldname": "fee_schedule", "fieldtype": "Link",
		 "options": "Fee Schedule", "width": 140},
		{"label": _("Invoice"), "fieldname": "invoice", "fieldtype": "Link",
		 "options": "Sales Invoice", "width": 150},
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date",
		 "width": 110},
		{"label": _("Due Date"), "fieldname": "due_date", "fieldtype": "Date", "width": 110},
		{"label": _("Paid Amount"), "fieldname": "paid_amount", "fieldtype": "Currency",
		 "options": "currency", "width": 130},
		{"label": _("Outstanding Amount"), "fieldname": "outstanding_amount",
		 "fieldtype": "Currency", "options": "currency", "width": 150},
		{"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency",
		 "options": "currency", "width": 130},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 120},
		fr.currency_column(),
	]


def get_conditions(filters):
	"""Shared with the whitelisted API so the web page and the desk report
	can never drift apart — one of them changing its filters silently would be
	worse than either being wrong."""
	conditions, params = fr.base_conditions(filters, FILTER_KEYS)
	params["guardian"] = filters.get("guardian")
	return fr.where(conditions), params


def fetch_rows(filters):
	"""Every school invoice belonging to a child of this guardian.

	The guardian link is an EXISTS rather than a join. Joining `Student
	Guardian` returns one row per matching child row, so a guardian listed
	twice on the same student — which nothing prevents — would return that
	child's invoices twice and double the family's balance on screen.
	"""
	where, params = get_conditions(filters)

	return frappe.db.sql(
		f"""
		SELECT
			s.name                   AS student,
			s.student_name           AS student_name,
			si.ms_academic_year      AS academic_year,
			si.ms_academic_term      AS academic_term,
			si.ms_program            AS program,
			pe.student_batch_name    AS student_batch_name,
			pe.student_category      AS student_category,
			si.fee_schedule          AS fee_schedule,
			si.name                  AS invoice,
			si.posting_date          AS posting_date,
			si.due_date              AS due_date,
			si.currency              AS currency,
			{fr.PAID_AMOUNT}         AS paid_amount,
			si.outstanding_amount    AS outstanding_amount,
			si.grand_total           AS grand_total,
			{fr.STATUS_CASE}         AS status
		  FROM `tabSales Invoice` si
		 INNER JOIN `tabStudent` s ON s.name = si.student
		  {fr.ENROLLMENT_JOIN}
		 WHERE EXISTS (
			SELECT 1 FROM `tabStudent Guardian` sg
			 WHERE sg.parent = s.name
			   AND sg.parenttype = 'Student'
			   AND sg.guardian = %(guardian)s
		   )
		   AND {where}
		 ORDER BY s.student_name, si.ms_academic_year, si.ms_academic_term, si.posting_date
		""",
		params,
		as_dict=True,
	)


def get_data(filters):
	if not filters.get("guardian"):
		frappe.throw(_("Please select a Guardian"))

	return build_tree(fetch_rows(filters))


def build_tree(rows):
	"""Nest the invoices under Student -> Year -> Term with running subtotals.

	`indent` drives the report view's tree; the totals are accumulated in one
	pass so a family with hundreds of invoices costs one walk, not one query
	per level.
	"""
	data = []
	currency = fr.default_currency()

	student = year = term = None
	student_name = None
	term_tot, year_tot, student_tot, grand_tot = _t(), _t(), _t(), _t()

	def flush(label, bucket, indent, kind):
		if bucket["count"]:
			data.append(_subtotal_row(label, bucket, indent, currency, kind))

	for r in rows:
		if student is not None and r.student != student:
			flush(_("Term Subtotal: {0}").format(term or "-"), term_tot, 3, "term")
			flush(_("Year Subtotal: {0}").format(year or "-"), year_tot, 2, "year")
			flush(_("Student Total: {0}").format(student_name or "-"), student_tot, 1, "student")
			term_tot, year_tot, student_tot = _t(), _t(), _t()
			term = year = None

		if r.student != student:
			student, student_name = r.student, r.student_name
			data.append({
				"student": r.student,
				"student_name": r.student_name,
				"currency": r.currency or currency,
				"indent": 0,
				"is_group": 1,
				"group_level": "student",
			})

		if r.academic_year != year:
			flush(_("Term Subtotal: {0}").format(term or "-"), term_tot, 3, "term")
			term_tot = _t()
			if year is not None:
				flush(_("Year Subtotal: {0}").format(year or "-"), year_tot, 2, "year")
				year_tot = _t()
			year, term = r.academic_year, None
			data.append({
				"student_name": "",
				"academic_year": r.academic_year,
				"currency": r.currency or currency,
				"indent": 1,
				"is_group": 1,
				"group_level": "year",
			})

		if r.academic_term != term:
			flush(_("Term Subtotal: {0}").format(term or "-"), term_tot, 3, "term")
			term_tot = _t()
			term = r.academic_term
			data.append({
				"student_name": "",
				"academic_term": r.academic_term,
				"currency": r.currency or currency,
				"indent": 2,
				"is_group": 1,
				"group_level": "term",
			})

		r["indent"] = 3
		r["currency"] = r.currency or currency
		data.append(r)
		for bucket in (term_tot, year_tot, student_tot, grand_tot):
			_accumulate(bucket, r)

	if student is not None:
		flush(_("Term Subtotal: {0}").format(term or "-"), term_tot, 3, "term")
		flush(_("Year Subtotal: {0}").format(year or "-"), year_tot, 2, "year")
		flush(_("Student Total: {0}").format(student_name or "-"), student_tot, 1, "student")

	flush(_("Grand Total"), grand_tot, 0, "grand")

	return data


def _t():
	return {"count": 0, "paid_amount": 0, "outstanding_amount": 0, "grand_total": 0}


def _accumulate(bucket, row):
	bucket["count"] += 1
	bucket["paid_amount"] += row.get("paid_amount") or 0
	bucket["outstanding_amount"] += row.get("outstanding_amount") or 0
	bucket["grand_total"] += row.get("grand_total") or 0


def _subtotal_row(label, bucket, indent, currency, kind):
	return {
		"student_name": label,
		"paid_amount": bucket["paid_amount"],
		"outstanding_amount": bucket["outstanding_amount"],
		"grand_total": bucket["grand_total"],
		"currency": currency,
		"indent": indent,
		"is_subtotal": 1,
		"subtotal_kind": kind,
	}
