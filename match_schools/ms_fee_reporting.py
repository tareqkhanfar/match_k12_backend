# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""One place that knows how a school fee is stored, for the desk reports.

v16 bills school fees as **Sales Invoices** raised against the student's own
Customer. The old `Fees` doctype is retired (see
`match_schools/patches/retire_legacy_fees.py`), so every report here reads
Sales Invoice and nothing else — a report that still queried `tabFees` would
quietly show a school its pre-migration numbers.

`api/receivables.py` is the equivalent for the mobile API. This module is its
SQL sibling: the desk reports need joins and grouping that `frappe.get_all`
cannot express, so the shape is defined once here rather than copy-pasted into
seven report files.

What a school invoice is:

    a Sales Invoice with `student` set, and docstatus = 1

Everything else on Sales Invoice is ordinary trade and must never reach these
reports, which is what `SCHOOL_INVOICE` guarantees.
"""

import frappe
from frappe import _

# --- What counts as a school invoice ---------------------------------------

#: Selects school invoices only. Every report's WHERE clause starts with this.
SCHOOL_INVOICE = "IFNULL(si.student, '') != ''"

#: Money is compared against a tolerance, never against an exact zero. An
#: invoice settled to the last fils routinely leaves a rounding crumb behind,
#: and `outstanding_amount = 0` reports such an invoice as *unpaid* — which is
#: how a fully-collected school ends up chasing families who owe nothing.
EPSILON = 0.005

#: Payment state, computed in SQL so it can also be filtered and grouped on.
STATUS_CASE = f"""
	CASE
		WHEN si.outstanding_amount <= {EPSILON} THEN 'Fully Paid'
		WHEN (si.grand_total - si.outstanding_amount) <= {EPSILON} THEN 'Unpaid'
		ELSE 'Partially Paid'
	END
"""

#: Amount actually collected. Taken as billed-minus-outstanding rather than by
#: summing Payment Entries, because ERPNext also clears a receivable through
#: Journal Entries, credit notes and payment reconciliation. Summing payments
#: would under-report every fee settled any other way.
PAID_AMOUNT = "(si.grand_total - si.outstanding_amount)"

STATUS_OPTIONS = ("All", "Fully Paid", "Partially Paid", "Unpaid")

STATUS_AR = {
	"Fully Paid": "مدفوع بالكامل",
	"Partially Paid": "مدفوع جزئياً",
	"Unpaid": "غير مدفوع",
	"No Fees": "بدون رسوم",
}


# --- Joins -----------------------------------------------------------------

#: The enrolment carries the batch and category that the old `Fees` doctype
#: stored on itself. Sales Invoice does not, so reports that show them join
#: through the enrolment `ms_billing` guarantees is present on every school
#: invoice. LEFT, not INNER: an invoice whose enrolment was later deleted must
#: still appear on the report, with blank batch/category, rather than vanish
#: from the school's receivables.
ENROLLMENT_JOIN = (
	"LEFT JOIN `tabProgram Enrollment` pe ON pe.name = si.ms_program_enrollment"
)

STUDENT_JOIN = "LEFT JOIN `tabStudent` s ON s.name = si.student"

#: Guardians hang off the Student, not off the invoice.
GUARDIAN_JOIN = """
	LEFT JOIN `tabStudent Guardian` sg
		ON sg.parent = si.student AND sg.parenttype = 'Student' AND sg.idx = 1
	LEFT JOIN `tabGuardian` g ON g.name = sg.guardian
"""


# --- Condition building ----------------------------------------------------

#: Filter name -> the column it constrains. Reports pass the subset they offer.
FILTER_COLUMNS = {
	"student": "si.student",
	"company": "si.company",
	"program": "si.ms_program",
	"academic_year": "si.ms_academic_year",
	"academic_term": "si.ms_academic_term",
	"program_enrollment": "si.ms_program_enrollment",
	"fee_schedule": "si.fee_schedule",
	"cost_center": "si.cost_center",
	"student_batch_name": "pe.student_batch_name",
	"student_category": "pe.student_category",
}


def base_conditions(filters, keys=None, apply_status=True):
	"""WHERE fragments and parameters for the filters a report offers.

	Returns `(conditions, params)`. `conditions` always starts with the two
	clauses that make the result a fee report at all — a named student and a
	posted invoice — so a caller cannot accidentally build a query that also
	returns the school's stationery purchases or a half-typed draft.

	`apply_status=False` leaves the payment-status filter out of the SQL, for
	reports that group before they judge. On a per-invoice report "Fully Paid"
	means *this invoice* is settled and belongs in the WHERE clause. On a
	report grouped by student it means *this student owes nothing*, which
	cannot be known until their invoices have been summed — filtering first
	would throw away the paid half of a part-paid student and then report the
	remainder as fully paid. Those reports filter after aggregating instead.
	"""
	filters = frappe._dict(filters or {})
	conditions = ["si.docstatus = 1", SCHOOL_INVOICE]
	params = {}

	for key in keys or FILTER_COLUMNS:
		column = FILTER_COLUMNS.get(key)
		value = filters.get(key)
		if not column or value in (None, "", []):
			continue
		conditions.append(f"{column} = %({key})s")
		params[key] = value

	add_date_range(filters, conditions, params, "si.posting_date", "from_date", "to_date")
	add_date_range(filters, conditions, params, "si.due_date", "due_from", "due_to")
	if apply_status:
		add_status_condition(filters.get("status"), conditions)
	add_outstanding_condition(filters.get("outstanding_amount"), conditions)
	add_student_group_condition(filters, conditions, params)

	return conditions, params


def add_date_range(filters, conditions, params, column, from_key, to_key):
	"""Inclusive on both ends — what a user means by "1 March to 31 March"."""
	if filters.get(from_key):
		conditions.append(f"{column} >= %({from_key})s")
		params[from_key] = filters[from_key]
	if filters.get(to_key):
		conditions.append(f"{column} <= %({to_key})s")
		params[to_key] = filters[to_key]


def add_status_condition(status, conditions):
	if not status or status == "All":
		return
	if status == "Fully Paid":
		conditions.append(f"si.outstanding_amount <= {EPSILON}")
	elif status == "Unpaid":
		conditions.append(f"(si.grand_total - si.outstanding_amount) <= {EPSILON}")
	elif status == "Partially Paid":
		conditions.append(
			f"si.outstanding_amount > {EPSILON} "
			f"AND (si.grand_total - si.outstanding_amount) > {EPSILON}"
		)


def add_outstanding_condition(choice, conditions):
	if choice == "Has Outstanding":
		conditions.append(f"si.outstanding_amount > {EPSILON}")
	elif choice == "Fully Paid":
		conditions.append(f"si.outstanding_amount <= {EPSILON}")


def add_student_group_condition(filters, conditions, params):
	if not filters.get("student_group"):
		return
	conditions.append(
		"si.student IN (SELECT sgs.student FROM `tabStudent Group Student` sgs "
		"WHERE sgs.parent = %(student_group)s)"
	)
	params["student_group"] = filters["student_group"]


def where(conditions):
	return " AND ".join(conditions)


# --- Payments --------------------------------------------------------------


def last_payments(invoices):
	"""The most recent receipt against each invoice, in one query.

	Batched deliberately: the version of this report that ran a query per row
	made one round trip per invoice, so a school with 4,000 invoices issued
	4,000 queries to fill three columns.
	"""
	if not invoices:
		return {}

	rows = frappe.db.sql(
		"""
		SELECT per.reference_name AS invoice,
		       pe.name            AS voucher,
		       pe.posting_date    AS posting_date,
		       pe.mode_of_payment AS mode_of_payment,
		       per.allocated_amount AS amount
		  FROM `tabPayment Entry Reference` per
		 INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		 WHERE per.reference_doctype = 'Sales Invoice'
		   AND per.docstatus = 1
		   AND pe.docstatus = 1
		   AND per.reference_name IN %(invoices)s
		 ORDER BY pe.posting_date DESC, pe.creation DESC
		""",
		{"invoices": list(invoices)},
		as_dict=True,
	)

	# Ordered newest first, so the first row seen for an invoice is its latest.
	latest = {}
	for r in rows:
		latest.setdefault(r.invoice, r)
	return latest


# --- Student ledger --------------------------------------------------------


def ledger_parties(student):
	"""Every party a student's money has ever been booked against.

	A school that has been through the v16 migration has two generations of
	postings: the legacy `Fees` documents sat on `party_type = 'Student'`,
	while a Sales Invoice books its receivable against the student's Customer.
	A statement that reads only one of them shows half the account — so both
	are collected here and the caller queries for either.
	"""
	parties = [("Student", student)]

	customer = frappe.db.get_value("Student", student, "customer")
	if customer:
		parties.append(("Customer", customer))

	return parties


def party_clause(parties, prefix="p"):
	"""`(party_type, party) IN (...)` as SQL, with its parameters.

	Written as an OR of pairs rather than `party IN (...)`, because a Customer
	and a Student can share a name and matching on the name alone would pull a
	stranger's ledger into a child's statement.
	"""
	clauses, params = [], {}
	for i, (party_type, party) in enumerate(parties):
		clauses.append(f"(gle.party_type = %({prefix}t{i})s AND gle.party = %({prefix}n{i})s)")
		params[f"{prefix}t{i}"] = party_type
		params[f"{prefix}n{i}"] = party
	return "(" + " OR ".join(clauses) + ")", params


# --- Presentation ----------------------------------------------------------


def currency_column():
	"""Hidden column the Currency columns point at via their `options`."""
	return {
		"fieldname": "currency",
		"fieldtype": "Link",
		"options": "Currency",
		"hidden": 1,
	}


def default_currency():
	return frappe.db.get_default("currency")


def money_summary(rows, total_key, paid_key, outstanding_key):
	"""The three cards every fee report shows above its grid."""
	currency = default_currency()
	total = sum(r.get(total_key) or 0 for r in rows)
	paid = sum(r.get(paid_key) or 0 for r in rows)
	outstanding = sum(r.get(outstanding_key) or 0 for r in rows)
	return [
		{"label": _("Grand Total"), "value": total, "datatype": "Currency",
		 "currency": currency, "indicator": "Blue"},
		{"label": _("Paid Amount"), "value": paid, "datatype": "Currency",
		 "currency": currency, "indicator": "Green"},
		{"label": _("Outstanding Amount"), "value": outstanding, "datatype": "Currency",
		 "currency": currency, "indicator": "Red"},
	]


def paid_outstanding_chart(rows, paid_key, outstanding_key):
	return {
		"data": {
			"labels": [_("Paid"), _("Outstanding")],
			"datasets": [
				{
					"name": _("Amount"),
					"values": [
						sum(r.get(paid_key) or 0 for r in rows),
						sum(r.get(outstanding_key) or 0 for r in rows),
					],
				}
			],
		},
		"type": "donut",
		"colors": ["#28a745", "#dc3545"],
	}
