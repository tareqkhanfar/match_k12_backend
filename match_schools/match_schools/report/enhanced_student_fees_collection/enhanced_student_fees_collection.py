# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Every school fee invoice with the family's contact details beside it.

The registrar's chase list: one row per invoice, showing what was billed, what
came in, what is still owed, and the phone number to ring about it.

Reads Sales Invoice, the v16 fee model. Batch and category, which the retired
`Fees` doctype carried on itself, come from the Program Enrollment the invoice
names.
"""

import frappe
from frappe import _
from frappe.utils import flt

from match_schools import ms_fee_reporting as fr

FILTER_KEYS = (
	"student",
	"company",
	"program",
	"academic_year",
	"academic_term",
	"fee_schedule",
	"student_batch_name",
	"student_category",
)


def execute(filters=None):
	filters = frappe._dict(filters or {})

	data = get_data(filters)
	return (
		get_columns(),
		data,
		None,
		get_chart(data),
		fr.money_summary(data, "grand_total", "paid_amount", "outstanding_amount"),
	)


def get_columns():
	return [
		{"label": _("Invoice"), "fieldname": "name", "fieldtype": "Link",
		 "options": "Sales Invoice", "width": 150},
		{"label": _("Student"), "fieldname": "student", "fieldtype": "Link",
		 "options": "Student", "width": 120},
		{"label": _("Student Name"), "fieldname": "student_name", "fieldtype": "Data",
		 "width": 180},
		{"label": _("Student Mobile"), "fieldname": "student_mobile", "fieldtype": "Data",
		 "width": 120},
		{"label": _("Guardian"), "fieldname": "guardian", "fieldtype": "Link",
		 "options": "Guardian", "width": 120},
		{"label": _("Guardian Name"), "fieldname": "guardian_name", "fieldtype": "Data",
		 "width": 160},
		{"label": _("Guardian Mobile"), "fieldname": "guardian_mobile", "fieldtype": "Data",
		 "width": 120},
		{"label": _("Program"), "fieldname": "program", "fieldtype": "Link",
		 "options": "Program", "width": 130},
		{"label": _("Academic Year"), "fieldname": "academic_year", "fieldtype": "Link",
		 "options": "Academic Year", "width": 110},
		{"label": _("Academic Term"), "fieldname": "academic_term", "fieldtype": "Link",
		 "options": "Academic Term", "width": 130},
		{"label": _("Batch"), "fieldname": "student_batch_name", "fieldtype": "Link",
		 "options": "Student Batch Name", "width": 110},
		{"label": _("Category"), "fieldname": "student_category", "fieldtype": "Link",
		 "options": "Student Category", "width": 120},
		{"label": _("Program Enrollment"), "fieldname": "program_enrollment",
		 "fieldtype": "Link", "options": "Program Enrollment", "width": 150},
		{"label": _("Fee Schedule"), "fieldname": "fee_schedule", "fieldtype": "Link",
		 "options": "Fee Schedule", "width": 150},
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date",
		 "width": 105},
		{"label": _("Due Date"), "fieldname": "due_date", "fieldtype": "Date", "width": 105},
		{"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency",
		 "options": "currency", "width": 120},
		{"label": _("Paid Amount"), "fieldname": "paid_amount", "fieldtype": "Currency",
		 "options": "currency", "width": 120},
		{"label": _("Outstanding Amount"), "fieldname": "outstanding_amount",
		 "fieldtype": "Currency", "options": "currency", "width": 140},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 120},
		{"label": _("Last Payment Date"), "fieldname": "last_payment_date",
		 "fieldtype": "Date", "width": 125},
		{"label": _("Last Payment Amount"), "fieldname": "last_payment_amount",
		 "fieldtype": "Currency", "options": "currency", "width": 145},
		{"label": _("Last Payment Voucher"), "fieldname": "last_payment_voucher",
		 "fieldtype": "Link", "options": "Payment Entry", "width": 160},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link",
		 "options": "Company", "width": 130},
		fr.currency_column(),
	]


def get_data(filters):
	conditions, params = fr.base_conditions(filters, FILTER_KEYS)

	rows = frappe.db.sql(
		f"""
		SELECT
			si.name                     AS name,
			si.student                  AS student,
			s.student_name              AS student_name,
			s.student_mobile_number     AS student_mobile,
			sg.guardian                 AS guardian,
			sg.guardian_name            AS guardian_name,
			g.mobile_number             AS guardian_mobile,
			si.ms_program               AS program,
			si.ms_academic_year         AS academic_year,
			si.ms_academic_term         AS academic_term,
			si.ms_program_enrollment    AS program_enrollment,
			pe.student_batch_name       AS student_batch_name,
			pe.student_category         AS student_category,
			si.fee_schedule             AS fee_schedule,
			si.posting_date             AS posting_date,
			si.due_date                 AS due_date,
			si.currency                 AS currency,
			si.company                  AS company,
			si.grand_total              AS grand_total,
			{fr.PAID_AMOUNT}            AS paid_amount,
			si.outstanding_amount       AS outstanding_amount,
			{fr.STATUS_CASE}            AS status
		  FROM `tabSales Invoice` si
		  {fr.STUDENT_JOIN}
		  {fr.ENROLLMENT_JOIN}
		  {fr.GUARDIAN_JOIN}
		 WHERE {fr.where(conditions)}
		 ORDER BY si.posting_date DESC, si.creation DESC
		""",
		params,
		as_dict=True,
	)

	attach_last_payment(rows)
	return rows


def attach_last_payment(rows):
	"""The most recent receipt, so the office can see when a family last paid."""
	latest = fr.last_payments([r.name for r in rows])
	for row in rows:
		payment = latest.get(row.name)
		row.last_payment_voucher = payment.voucher if payment else None
		row.last_payment_date = payment.posting_date if payment else None
		row.last_payment_amount = flt(payment.amount) if payment else 0


def get_chart(rows):
	if not rows:
		return None
	return {
		"data": {
			"labels": [_("Billed"), _("Collected"), _("Outstanding")],
			"datasets": [
				{
					"name": _("Amount"),
					"values": [
						sum(flt(r.grand_total) for r in rows),
						sum(flt(r.paid_amount) for r in rows),
						sum(flt(r.outstanding_amount) for r in rows),
					],
				}
			],
		},
		"type": "bar",
		"colors": ["#4285F4", "#0F9D58", "#DB4437"],
		"barOptions": {"stacked": 0},
	}
