# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Fee collection rolled up, grouped by whatever the office is asking about.

Billed, collected and outstanding summed across a chosen dimension — a
student, a programme, a term, a batch — with a collection rate beside each row
so a weak grade shows up without arithmetic.

Reads Sales Invoice, the v16 fee model.

Two things changed against the v15 version, both because it was wrong rather
than because v16 forced it:

  * it joined `Fees` to *every* Program Enrollment the student had ever held,
    so a child enrolled in three years had their fees counted three times. The
    programme now comes from the invoice itself (`ms_program`), which is the
    enrolment the fee was actually raised against, so no join is needed.
  * it selected un-aggregated columns alongside a GROUP BY, which MariaDB
    either rejects under ONLY_FULL_GROUP_BY or answers with an arbitrary row.
    Every non-grouped column is now a real SUM.
"""

import frappe
from frappe import _
from frappe.utils import flt

from match_schools import ms_fee_reporting as fr

FILTER_KEYS = (
	"company",
	"program",
	"academic_year",
	"academic_term",
	"student",
	"fee_schedule",
	"student_batch_name",
	"student_category",
)

#: What each grouping is keyed and labelled by. The label column is a Link
#: wherever the value is one, so a row stays clickable through to the record.
#: Labels are stored untranslated and passed through `_()` in `get_columns`:
#: calling `_()` at import time resolves against whatever language happened to
#: be active when the worker first loaded the module, which is how a report
#: ends up permanently English for an Arabic user.
GROUPINGS = {
	"Student": {
		"expression": "si.student",
		"label": "Student",
		"fieldtype": "Link",
		"options": "Student",
		"name_expression": "MAX(s.student_name)",
	},
	"Program": {
		"expression": "si.ms_program",
		"label": "Program",
		"fieldtype": "Link",
		"options": "Program",
	},
	"Academic Year": {
		"expression": "si.ms_academic_year",
		"label": "Academic Year",
		"fieldtype": "Link",
		"options": "Academic Year",
	},
	"Academic Term": {
		"expression": "si.ms_academic_term",
		"label": "Academic Term",
		"fieldtype": "Link",
		"options": "Academic Term",
	},
	"Student Batch": {
		"expression": "pe.student_batch_name",
		"label": "Student Batch",
		"fieldtype": "Link",
		"options": "Student Batch Name",
	},
	"Student Category": {
		"expression": "pe.student_category",
		"label": "Student Category",
		"fieldtype": "Link",
		"options": "Student Category",
	},
	"Fee Schedule": {
		"expression": "si.fee_schedule",
		"label": "Fee Schedule",
		"fieldtype": "Link",
		"options": "Fee Schedule",
	},
}

DEFAULT_GROUP_BY = "Student"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	grouping = resolve_grouping(filters)

	data = get_data(filters, grouping)
	details = [d for d in data if not d.get("is_total")]

	return (
		get_columns(grouping),
		data,
		None,
		get_chart(details, grouping),
		fr.money_summary(details, "grand_total", "paid_amount", "outstanding_amount"),
	)


def resolve_grouping(filters):
	"""The chosen dimension, falling back rather than throwing on a stale
	saved filter — a report the user cannot open is worse than one grouped the
	default way."""
	return GROUPINGS.get(filters.get("group_by") or DEFAULT_GROUP_BY) or GROUPINGS[DEFAULT_GROUP_BY]


def get_columns(grouping):
	columns = [
		{
			"label": _(grouping["label"]),
			"fieldname": "group_value",
			"fieldtype": grouping["fieldtype"],
			"options": grouping.get("options"),
			"width": 180,
		}
	]

	if grouping.get("name_expression"):
		columns.append(
			{"label": _("Name"), "fieldname": "group_name", "fieldtype": "Data", "width": 220}
		)

	columns += [
		{"label": _("Invoices"), "fieldname": "invoices", "fieldtype": "Int", "width": 90},
		{"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency",
		 "options": "currency", "width": 140},
		{"label": _("Paid Amount"), "fieldname": "paid_amount", "fieldtype": "Currency",
		 "options": "currency", "width": 140},
		{"label": _("Outstanding Amount"), "fieldname": "outstanding_amount",
		 "fieldtype": "Currency", "options": "currency", "width": 160},
		{"label": _("Collection %"), "fieldname": "collection_rate", "fieldtype": "Percent",
		 "width": 110},
		{"label": _("Unpaid Invoices"), "fieldname": "unpaid_invoices", "fieldtype": "Int",
		 "width": 130},
		{"label": _("Earliest Due"), "fieldname": "earliest_due", "fieldtype": "Date",
		 "width": 120},
		{"label": _("Latest Posting"), "fieldname": "latest_posting", "fieldtype": "Date",
		 "width": 120},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 120},
		fr.currency_column(),
	]
	return columns


def get_data(filters, grouping):
	# `apply_status=False`: the status filter belongs after the grouping, not
	# in the WHERE clause. See `apply_post_filters`.
	conditions, params = fr.base_conditions(filters, FILTER_KEYS, apply_status=False)
	expression = grouping["expression"]
	name_select = grouping.get("name_expression")

	rows = frappe.db.sql(
		f"""
		SELECT
			{expression}                       AS group_value,
			{name_select or "NULL"}            AS group_name,
			COUNT(*)                           AS invoices,
			SUM(si.grand_total)                AS grand_total,
			SUM({fr.PAID_AMOUNT})              AS paid_amount,
			SUM(si.outstanding_amount)         AS outstanding_amount,
			SUM(CASE WHEN si.outstanding_amount > {fr.EPSILON} THEN 1 ELSE 0 END)
			                                   AS unpaid_invoices,
			MIN(CASE WHEN si.outstanding_amount > {fr.EPSILON} THEN si.due_date END)
			                                   AS earliest_due,
			MAX(si.posting_date)               AS latest_posting,
			MAX(si.currency)                   AS currency
		  FROM `tabSales Invoice` si
		  {fr.STUDENT_JOIN}
		  {fr.ENROLLMENT_JOIN}
		 WHERE {fr.where(conditions)}
		 GROUP BY {expression}
		 ORDER BY SUM(si.outstanding_amount) DESC, SUM(si.grand_total) DESC
		""",
		params,
		as_dict=True,
	)

	currency = fr.default_currency()
	for row in rows:
		row.currency = row.currency or currency
		row.collection_rate = (
			round(flt(row.paid_amount) / flt(row.grand_total) * 100, 2)
			if flt(row.grand_total)
			else 0.0
		)
		row.status = summarise_status(row)

	rows = apply_post_filters(rows, filters)
	if rows:
		rows.append(build_total_row(rows, currency))

	return rows


def summarise_status(row):
	"""One word for the whole group, not for a single invoice."""
	if flt(row.outstanding_amount) <= fr.EPSILON:
		return "Fully Paid"
	if flt(row.paid_amount) <= fr.EPSILON:
		return "Unpaid"
	return "Partially Paid"


def apply_post_filters(rows, filters):
	"""Filters that only mean something once the group has been summed.

	`status` cannot go into the WHERE clause here the way it does on the
	per-invoice reports: a student with one paid and one unpaid invoice is
	*partially* paid overall, and filtering the invoices first would have
	dropped the paid half and reported them as unpaid.
	"""
	status = filters.get("status") or "All"
	if status != "All":
		rows = [r for r in rows if r.status == status]

	if filters.get("only_with_outstanding"):
		rows = [r for r in rows if flt(r.outstanding_amount) > fr.EPSILON]

	return rows


def build_total_row(rows, currency):
	grand_total = sum(flt(r.grand_total) for r in rows)
	paid = sum(flt(r.paid_amount) for r in rows)
	return frappe._dict({
		"group_value": _("Total"),
		"group_name": _("{0} groups").format(len(rows)),
		"invoices": sum(r.invoices or 0 for r in rows),
		"grand_total": grand_total,
		"paid_amount": paid,
		"outstanding_amount": sum(flt(r.outstanding_amount) for r in rows),
		"unpaid_invoices": sum(r.unpaid_invoices or 0 for r in rows),
		"collection_rate": round(paid / grand_total * 100, 2) if grand_total else 0.0,
		"currency": currency,
		"is_total": 1,
	})


def get_chart(rows, grouping):
	if not rows:
		return None

	# The ten biggest debts — a chart of 400 students is a solid block of ink.
	top = sorted(rows, key=lambda r: flt(r.outstanding_amount), reverse=True)[:10]
	labels = [str(r.group_name or r.group_value or _("Not Set")) for r in top]

	return {
		"data": {
			"labels": labels,
			"datasets": [
				{"name": _("Collected"), "values": [flt(r.paid_amount) for r in top]},
				{"name": _("Outstanding"), "values": [flt(r.outstanding_amount) for r in top]},
			],
		},
		"type": "bar",
		"colors": ["#0F9D58", "#DB4437"],
		"barOptions": {"stacked": 1},
	}
