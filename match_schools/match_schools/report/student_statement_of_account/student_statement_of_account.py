# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""A student's ledger: everything billed, everything paid, running balance.

The document a family is handed when they ask for their account, and the one
an auditor asks for. Built from GL Entry rather than from invoices, so a
journal correction, a write-off or a credit note appears exactly where it
happened instead of being invisible.

The v16 move is the whole point of this file. Under the retired `Fees`
doctype the receivable sat on `party_type = 'Student'`. A Sales Invoice books
it against the **Customer** linked to the student. A school that has migrated
therefore has two generations of postings, and a statement reading either one
alone shows half an account — an opening balance that does not tie, and a
family told they owe nothing when they owe last year's fees.

So both parties are read together (see `ms_fee_reporting.ledger_parties`), and
the statement is continuous across the migration.
"""

import frappe
from frappe import _
from frappe.utils import flt

from match_schools import ms_fee_reporting as fr


def execute(filters=None):
	filters = frappe._dict(filters or {})

	if not filters.company:
		frappe.throw(_("Please select a Company"))
	if not filters.student:
		frappe.throw(_("Please select a Student"))

	data = get_data(filters)
	return get_columns(), data, None, None, get_report_summary(data)


def get_columns():
	return [
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date",
		 "width": 100},
		{"label": _("Account"), "fieldname": "account", "fieldtype": "Link",
		 "options": "Account", "width": 200},
		{"label": _("Debit"), "fieldname": "debit", "fieldtype": "Currency",
		 "options": "currency", "width": 120},
		{"label": _("Credit"), "fieldname": "credit", "fieldtype": "Currency",
		 "options": "currency", "width": 120},
		{"label": _("Balance"), "fieldname": "balance", "fieldtype": "Currency",
		 "options": "currency", "width": 130},
		{"label": _("Voucher Type"), "fieldname": "voucher_type", "fieldtype": "Data",
		 "width": 130},
		{"label": _("Voucher No"), "fieldname": "voucher_no", "fieldtype": "Dynamic Link",
		 "options": "voucher_type", "width": 170},
		{"label": _("Party"), "fieldname": "party_label", "fieldtype": "Data", "width": 150},
		{"label": _("Against Account"), "fieldname": "against", "fieldtype": "Data",
		 "width": 160, "hidden": 1},
		{"label": _("Against Voucher"), "fieldname": "against_voucher",
		 "fieldtype": "Dynamic Link", "options": "against_voucher_type", "width": 160,
		 "hidden": 1},
		{"label": _("Row Type"), "fieldname": "row_type", "fieldtype": "Data", "width": 110,
		 "hidden": 1},
		{"label": _("Remarks"), "fieldname": "remarks", "fieldtype": "Data", "width": 220},
		{"fieldname": "against_voucher_type", "fieldtype": "Data", "hidden": 1},
		fr.currency_column(),
	]


def get_vouchers(filters, party_sql, party_params):
	"""Every voucher that touches this student's account, up to `to_date`.

	Collected first so the detail query can pull the *other* legs of the same
	vouchers — the cash, bank or discount account the money moved to — which
	is what makes the statement readable rather than a column of receivable
	postings with no counterparty.
	"""
	conditions = ["gle.company = %(company)s", "gle.is_cancelled = 0", party_sql]
	params = {"company": filters.company, **party_params}

	if filters.to_date:
		conditions.append("gle.posting_date <= %(to_date)s")
		params["to_date"] = filters.to_date

	return frappe.db.sql(
		f"""
		SELECT DISTINCT gle.voucher_type, gle.voucher_no
		  FROM `tabGL Entry` gle
		 WHERE {fr.where(conditions)}
		""",
		params,
		as_dict=True,
	)


def get_opening_balance(filters, party_sql, party_params):
	"""Net of the student's own party rows before `from_date`.

	Taken straight from the ledger rather than from the visible rows: an
	opening balance derived from the page you are looking at is not an opening
	balance, it is a subtotal.
	"""
	if not filters.from_date:
		return 0.0

	row = frappe.db.sql(
		f"""
		SELECT SUM(gle.debit) - SUM(gle.credit) AS balance
		  FROM `tabGL Entry` gle
		 WHERE gle.company = %(company)s
		   AND gle.is_cancelled = 0
		   AND gle.posting_date < %(from_date)s
		   AND {party_sql}
		""",
		{"company": filters.company, "from_date": filters.from_date, **party_params},
		as_dict=True,
	)
	return flt(row[0].balance) if row else 0.0


def get_data(filters):
	parties = fr.ledger_parties(filters.student)
	party_sql, party_params = fr.party_clause(parties)
	# The pairs the student "owns", used to decide which rows move the balance.
	own_parties = {(t, n) for t, n in parties}

	vouchers = get_vouchers(filters, party_sql, party_params)
	if not vouchers:
		return []

	voucher_nos = [v.voucher_no for v in vouchers]

	conditions = [
		"gle.company = %(company)s",
		"gle.is_cancelled = 0",
		"gle.voucher_no IN %(voucher_nos)s",
	]
	params = {"company": filters.company, "voucher_nos": voucher_nos}

	if not filters.show_all_voucher_accounts:
		conditions.append(party_sql)
		params.update(party_params)

	if filters.from_date:
		conditions.append("gle.posting_date >= %(from_date)s")
		params["from_date"] = filters.from_date
	if filters.to_date:
		conditions.append("gle.posting_date <= %(to_date)s")
		params["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		SELECT
			gle.posting_date          AS posting_date,
			gle.account               AS account,
			gle.debit                 AS debit,
			gle.credit                AS credit,
			gle.voucher_type          AS voucher_type,
			gle.voucher_no            AS voucher_no,
			gle.against               AS against,
			gle.against_voucher_type  AS against_voucher_type,
			gle.against_voucher       AS against_voucher,
			gle.party_type            AS party_type,
			gle.party                 AS party,
			gle.remarks               AS remarks,
			gle.account_currency      AS currency
		  FROM `tabGL Entry` gle
		 WHERE {fr.where(conditions)}
		 ORDER BY gle.posting_date, gle.creation
		""",
		params,
		as_dict=True,
	)

	return build_statement(rows, filters, own_parties)


def build_statement(rows, filters, own_parties):
	currency = fr.default_currency()
	opening = get_opening_balance(filters, *fr.party_clause(list(own_parties)))

	data = []
	if filters.from_date:
		data.append({
			"account": _("Opening Balance"),
			"debit": opening if opening > 0 else 0,
			"credit": -opening if opening < 0 else 0,
			"balance": opening,
			"currency": currency,
			"row_type": "opening",
		})

	balance = opening
	total_debit = total_credit = 0.0

	for r in rows:
		# Only the student's own receivable leg moves the balance. The cash and
		# discount legs of the same voucher are shown for context and would
		# double every figure if they were added in.
		is_own = (r.party_type, r.party) in own_parties
		if is_own:
			balance += flt(r.debit) - flt(r.credit)
			total_debit += flt(r.debit)
			total_credit += flt(r.credit)

		data.append({
			"posting_date": r.posting_date,
			"account": r.account,
			"debit": r.debit,
			"credit": r.credit,
			"balance": balance if is_own else None,
			"voucher_type": r.voucher_type,
			"voucher_no": r.voucher_no,
			"party_label": party_label(r, is_own),
			"against": r.against,
			"against_voucher_type": r.against_voucher_type,
			"against_voucher": r.against_voucher,
			"remarks": r.remarks,
			"currency": r.currency or currency,
			"row_type": "student" if is_own else "other_leg",
		})

	data.append({
		"account": _("Closing Balance"),
		"debit": total_debit,
		"credit": total_credit,
		"balance": balance,
		"currency": currency,
		"row_type": "closing",
	})

	return data


def party_label(row, is_own):
	"""Shows which of the two generations a row came from.

	Worth a column: when a balance looks wrong, the first question is whether
	it is a legacy `Fees` posting against the Student or an invoice against
	the Customer, and this answers it without opening the voucher.
	"""
	if not row.party:
		return ""
	if is_own and row.party_type == "Student":
		return _("Student (legacy fee)")
	if is_own and row.party_type == "Customer":
		return _("Customer")
	return f"{row.party_type}: {row.party}"


def get_report_summary(data):
	closing = next((d for d in data if d.get("row_type") == "closing"), None)
	if not closing:
		return []

	currency = fr.default_currency()
	return [
		{"label": _("Total Debit"), "value": closing["debit"], "datatype": "Currency",
		 "currency": currency, "indicator": "Orange"},
		{"label": _("Total Credit"), "value": closing["credit"], "datatype": "Currency",
		 "currency": currency, "indicator": "Green"},
		{"label": _("Closing Balance (Due)"), "value": closing["balance"],
		 "datatype": "Currency", "currency": currency,
		 "indicator": "Red" if closing["balance"] > 0 else "Green"},
	]
