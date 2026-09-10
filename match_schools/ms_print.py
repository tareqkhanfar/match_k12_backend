# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""What the school print formats need that the document does not carry.

A fee receipt and a fee invoice both have to name the family, not just the
Customer record the money is booked against — a parent handed a slip saying
"سلمى سامي حمدان 3" cannot tell whose fees it settles. The guardian is two
hops away (invoice → student → Student Guardian → Guardian), and the running
balance is not on the document at all.

Doing that in the template is the wrong place: the Jinja sandbox turns a typo
into a silently blank cell, and the same three hops would be copy-pasted into
every format. These are registered as Jinja methods in `hooks.py` instead, so
a print format asks one question and gets a dict back.

Registered individually rather than by module path, because Frappe exposes
*every* function in a hooked module — imports included — and `flt` does not
belong in a template's namespace.
"""

import frappe
from frappe.utils import flt

from match_schools import ms_fee_reporting as fr


def _guardians(student):
	"""Every guardian on the student, in the order the school listed them."""
	if not student:
		return []

	rows = frappe.get_all(
		"Student Guardian",
		filters={"parent": student, "parenttype": "Student"},
		fields=["guardian", "guardian_name", "relation"],
		order_by="idx",
	)

	out = []
	seen = set()
	for r in rows:
		# A guardian listed twice would print twice; nothing stops that being
		# entered, so it is de-duplicated here rather than on the slip.
		if r.guardian in seen:
			continue
		seen.add(r.guardian)

		details = frappe.db.get_value(
			"Guardian", r.guardian, ["guardian_name", "mobile_number", "email_address"],
			as_dict=True,
		) or {}
		out.append({
			"guardian": r.guardian,
			"name": r.guardian_name or details.get("guardian_name") or r.guardian,
			"relation": r.relation or "",
			"mobile": details.get("mobile_number") or "",
			"email": details.get("email_address") or "",
		})
	return out


def _guardian_names(guardians):
	return "، ".join(g["name"] for g in guardians if g.get("name"))


def _party_balance(student, party_type=None, party=None):
	"""The party's ledger balance right now, positive when they owe.

	Read from GL rather than by summing invoices, so a journal correction or a
	write-off is reflected. For a student both generations of postings count —
	the legacy `Fees` documents on the Student party and Sales Invoices on the
	Customer — for the same reason the statement report reads both.
	"""
	if student:
		parties = fr.ledger_parties(student)
	elif party_type and party:
		parties = [(party_type, party)]
	else:
		return 0.0

	clause, params = fr.party_clause(parties)
	row = frappe.db.sql(
		f"""
		SELECT IFNULL(SUM(gle.debit), 0) - IFNULL(SUM(gle.credit), 0) AS balance
		  FROM `tabGL Entry` gle
		 WHERE gle.is_cancelled = 0 AND {clause}
		""",
		params,
		as_dict=True,
	)
	return flt(row[0].balance) if row else 0.0


def ms_receipt_context(doc):
	"""School context for a Payment Entry print format.

	Returns a dict — never None — so a template can read `ctx.student_name`
	without guarding every access. An ordinary (non-school) receipt comes back
	with the student keys empty and the party balance still filled in, so the
	same format prints correctly for a supplier refund too.
	"""
	invoices = _receipt_invoices(doc)
	student = next((i["student"] for i in invoices if i["student"]), None)

	# A receipt taken on account, with nothing allocated yet, still belongs to
	# a student if the payer is that student's Customer.
	if not student and doc.get("party_type") == "Customer" and doc.get("party"):
		student = frappe.db.get_value("Student", {"customer": doc.party}, "name")
	if not student and doc.get("party_type") == "Student":
		student = doc.party

	guardians = _guardians(student)

	allocated = flt(doc.get("total_allocated_amount")) or flt(doc.get("paid_amount"))
	balance_now = _party_balance(student, doc.get("party_type"), doc.get("party"))

	# A submitted receipt is already in the ledger, so the balance read back is
	# the balance *after* it. A draft has posted nothing yet, so it is the
	# balance before. Getting this backwards prints a slip whose two figures
	# are swapped, which a parent will notice and an auditor will query.
	if doc.get("docstatus") == 1:
		balance_after = balance_now
		balance_before = balance_now + allocated
	else:
		balance_before = balance_now
		balance_after = balance_now - allocated

	return frappe._dict({
		"student": student,
		"student_name": frappe.db.get_value("Student", student, "student_name") if student else None,
		"guardians": guardians,
		"guardian_names": _guardian_names(guardians),
		"primary_guardian": guardians[0] if guardians else None,
		"invoices": invoices,
		"allocated": allocated,
		"balance_before": balance_before,
		"balance_after": balance_after,
		"received_from": _received_from(doc, guardians),
	})


def _received_from(doc, guardians):
	"""Who the slip says the money came from.

	The Customer behind a student is named after the child, so a receipt that
	prints only that reads as though the child paid. The guardian is named
	when there is one, with the customer kept alongside for the record.
	"""
	# Honour the custom field if a site has added one; v16 ships no such field,
	# so this is read defensively rather than assumed.
	explicit = doc.get("custom_i_received_from_mr")
	if explicit:
		return explicit
	if guardians:
		return guardians[0]["name"]
	return doc.get("party_name") or doc.get("party") or ""


def _receipt_invoices(doc):
	"""The invoices this receipt settles, with what each one still owes."""
	rows = []
	for ref in doc.get("references") or []:
		row = {
			"doctype": ref.reference_doctype,
			"name": ref.reference_name,
			"allocated": flt(ref.allocated_amount),
			"student": None,
			"posting_date": None,
			"due_date": None,
			"grand_total": flt(ref.get("total_amount")),
			"outstanding": flt(ref.get("outstanding_amount")),
			"description": "",
		}

		if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
			invoice = frappe.db.get_value(
				"Sales Invoice",
				ref.reference_name,
				["student", "posting_date", "due_date", "grand_total",
				 "outstanding_amount", "ms_program", "ms_academic_year", "ms_academic_term"],
				as_dict=True,
			)
			if invoice:
				row.update({
					"student": invoice.student,
					"posting_date": invoice.posting_date,
					"due_date": invoice.due_date,
					"grand_total": flt(invoice.grand_total),
					"outstanding": flt(invoice.outstanding_amount),
					"description": " — ".join(
						p for p in (invoice.ms_program, invoice.ms_academic_year,
						            invoice.ms_academic_term) if p
					),
				})

		rows.append(row)
	return rows


def ms_invoice_context(doc):
	"""School context for a Sales Invoice print format.

	Empty student keys on an ordinary trade invoice, so one format can be used
	for both without branching on the doctype.
	"""
	student = doc.get("student")
	guardians = _guardians(student)

	enrollment = {}
	if doc.get("ms_program_enrollment"):
		enrollment = frappe.db.get_value(
			"Program Enrollment",
			doc.ms_program_enrollment,
			["program", "academic_year", "academic_term", "student_batch_name",
			 "student_category"],
			as_dict=True,
		) or {}

	paid = flt(doc.get("grand_total")) - flt(doc.get("outstanding_amount"))

	return frappe._dict({
		"student": student,
		"student_name": frappe.db.get_value("Student", student, "student_name") if student else None,
		"id_number": frappe.db.get_value("Student", student, "ms_id_number") if student else None,
		"mobile": frappe.db.get_value("Student", student, "student_mobile_number") if student else None,
		"guardians": guardians,
		"guardian_names": _guardian_names(guardians),
		"primary_guardian": guardians[0] if guardians else None,
		"program": doc.get("ms_program") or enrollment.get("program"),
		"academic_year": doc.get("ms_academic_year") or enrollment.get("academic_year"),
		"academic_term": doc.get("ms_academic_term") or enrollment.get("academic_term"),
		"batch": enrollment.get("student_batch_name"),
		"category": enrollment.get("student_category"),
		"paid": paid,
		"outstanding": flt(doc.get("outstanding_amount")),
		"payments": _invoice_payments(doc),
		"balance": _party_balance(student, "Customer", doc.get("customer")),
	})


def _invoice_payments(doc):
	"""Receipts booked against this invoice, newest first."""
	if not doc.get("name"):
		return []

	rows = frappe.db.sql(
		"""
		SELECT pe.name            AS name,
		       pe.posting_date    AS posting_date,
		       pe.mode_of_payment AS mode_of_payment,
		       pe.reference_no    AS reference_no,
		       per.allocated_amount AS allocated
		  FROM `tabPayment Entry Reference` per
		 INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		 WHERE per.reference_doctype = 'Sales Invoice'
		   AND per.reference_name = %(invoice)s
		   AND pe.docstatus = 1
		 ORDER BY pe.posting_date DESC, pe.creation DESC
		""",
		{"invoice": doc.name},
		as_dict=True,
	)
	return [dict(r) for r in rows]
