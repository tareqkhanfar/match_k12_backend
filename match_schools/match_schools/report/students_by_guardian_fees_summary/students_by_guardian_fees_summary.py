# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""A guardian's children and their invoices, flat, with a total per child.

The shorter sibling of `Guardian Fees Statement`: same source, no year/term
nesting. Used when the office wants a printable one-pager per family rather
than a drill-down.

Reads Sales Invoice, the v16 fee model.
"""

import frappe
from frappe import _

from match_schools import ms_fee_reporting as fr

FILTER_KEYS = ("company", "program", "academic_year", "academic_term")


def execute(filters=None):
	filters = frappe._dict(filters or {})

	data = get_data(filters)
	details = [d for d in data if not d.get("is_subtotal")]

	return (
		get_columns(),
		data,
		None,
		fr.paid_outstanding_chart(details, "paid_amount", "outstanding_amount"),
		fr.money_summary(details, "grand_total", "paid_amount", "outstanding_amount"),
	)


def get_columns():
	return [
		{"label": _("Student"), "fieldname": "student", "fieldtype": "Link",
		 "options": "Student", "width": 140},
		{"label": _("Student Name"), "fieldname": "student_name", "fieldtype": "Data",
		 "width": 220},
		{"label": _("Invoice"), "fieldname": "invoice", "fieldtype": "Link",
		 "options": "Sales Invoice", "width": 150},
		{"label": _("Fee Schedule"), "fieldname": "fee_schedule", "fieldtype": "Link",
		 "options": "Fee Schedule", "width": 150},
		{"label": _("Program"), "fieldname": "program", "fieldtype": "Link",
		 "options": "Program", "width": 130},
		{"label": _("Academic Term"), "fieldname": "academic_term", "fieldtype": "Link",
		 "options": "Academic Term", "width": 130},
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
	conditions, params = fr.base_conditions(filters, FILTER_KEYS)
	params["guardian"] = filters.get("guardian")
	return fr.where(conditions), params


def get_data(filters):
	"""The guardian link is an EXISTS, not a join: a guardian listed twice on
	the same student would otherwise return that child's invoices twice."""
	if not filters.get("guardian"):
		frappe.throw(_("Please select a Guardian"))

	where, params = get_conditions(filters)

	rows = frappe.db.sql(
		f"""
		SELECT
			s.name                   AS student,
			s.student_name           AS student_name,
			si.name                  AS invoice,
			si.fee_schedule          AS fee_schedule,
			si.ms_program            AS program,
			si.ms_academic_term      AS academic_term,
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
		 ORDER BY s.student_name, si.posting_date
		""",
		params,
		as_dict=True,
	)

	return with_student_subtotals(rows)


def with_student_subtotals(rows):
	"""One "Total for <child>" row after each child's invoices."""
	currency = fr.default_currency()
	data = []
	student = student_name = None
	subtotal = _t()

	def flush():
		if subtotal["count"]:
			data.append({
				"student": student,
				"student_name": _("Total for {0}").format(student_name or student),
				"paid_amount": subtotal["paid_amount"],
				"outstanding_amount": subtotal["outstanding_amount"],
				"grand_total": subtotal["grand_total"],
				"currency": currency,
				"is_subtotal": 1,
			})

	for row in rows:
		if student is not None and row.student != student:
			flush()
			subtotal = _t()
		student, student_name = row.student, row.student_name
		row["currency"] = row.currency or currency
		data.append(row)
		subtotal["count"] += 1
		subtotal["paid_amount"] += row.paid_amount or 0
		subtotal["outstanding_amount"] += row.outstanding_amount or 0
		subtotal["grand_total"] += row.grand_total or 0

	if student is not None:
		flush()

	return data


def _t():
	return {"count": 0, "paid_amount": 0, "outstanding_amount": 0, "grand_total": 0}
