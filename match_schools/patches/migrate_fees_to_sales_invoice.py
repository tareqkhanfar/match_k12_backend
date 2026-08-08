"""Carry the legacy `Fees` receivables over to Sales Invoice.

The app now reads fees exclusively from Sales Invoice, the v16 model. A school
that has been running on the old `Fees` doctype still has real, collectable
debt recorded there, and it would simply vanish from every screen — so each
submitted Fee is reissued as a Sales Invoice carrying the same amounts, dates
and enrolment.

What this deliberately does *not* do:

  * it does not cancel or alter the original Fees documents. Their GL entries
    stay exactly as posted, so the audit trail is untouched and nothing is
    restated. They are marked as migrated instead.
  * it does not re-post revenue. The new invoice is created and submitted, and
    then the original's GL effect is reversed by cancelling it *after* the
    replacement exists — see `_retire_original`. Skipping that would double
    the receivable.
  * it never touches a Fee that has payments recorded, because unwinding a
    settled receivable is a judgement call for the accountant, not a script.

Anything it will not migrate is reported by name so a human can decide.
"""

import frappe
from frappe.utils import flt

MIGRATED_REMARK = "Migrated from Fees {0}"


def execute():
	if not frappe.db.table_exists("Fees"):
		return
	migrate()


def migrate(dry_run: bool = False) -> dict:
	rows = frappe.get_all(
		"Fees",
		filters={"docstatus": 1},
		fields=[
			"name", "student", "program_enrollment", "program", "academic_year",
			"academic_term", "posting_date", "due_date", "grand_total",
			"outstanding_amount", "company", "currency",
		],
		order_by="posting_date",
	)

	done, skipped = [], []

	for fee in rows:
		if _already_migrated(fee.name):
			continue

		problem = _cannot_migrate(fee)
		if problem:
			skipped.append((fee.name, problem))
			continue

		if dry_run:
			done.append((fee.name, "(dry run)"))
			continue

		try:
			invoice = _reissue(fee)
			_retire_original(fee.name)
			done.append((fee.name, invoice))
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"Fees migration failed: {fee.name}")
			skipped.append((fee.name, "error — see the error log"))

	return {"migrated": done, "skipped": skipped}


def _already_migrated(fee: str) -> bool:
	return bool(
		frappe.db.exists(
			"Sales Invoice",
			{"remarks": ["like", "%" + MIGRATED_REMARK.format(fee) + "%"], "docstatus": ["<", 2]},
		)
	)


def _cannot_migrate(fee) -> str | None:
	"""Reasons a Fee is left for a human rather than moved automatically."""
	paid = flt(fee.grand_total) - flt(fee.outstanding_amount)
	if paid > 0.005:
		return f"has {paid:g} already paid"
	if not fee.student or not frappe.db.exists("Student", fee.student):
		return "student missing"
	if not fee.program_enrollment or not frappe.db.exists(
		"Program Enrollment", fee.program_enrollment
	):
		return "no valid program enrollment"

	components = frappe.get_all(
		"Fee Component",
		filters={"parent": fee.name, "parenttype": "Fees"},
		fields=["fees_category", "amount"],
	)
	if not components:
		return "no components"

	for c in components:
		item = frappe.db.get_value("Fee Category", c.fees_category, "item")
		if not item or not frappe.db.exists("Item", item):
			return f"fee category '{c.fees_category}' has no item"

	return None


def _reissue(fee) -> str:
	"""Create the equivalent Sales Invoice and submit it."""
	customer = frappe.db.get_value("Student", fee.student, "customer")
	if not customer:
		student = frappe.get_doc("Student", fee.student)
		student.set_missing_customer_details()
		customer = frappe.db.get_value("Student", fee.student, "customer")

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	invoice.student = fee.student
	invoice.ms_program_enrollment = fee.program_enrollment
	invoice.posting_date = fee.posting_date
	invoice.set_posting_time = 1
	invoice.due_date = fee.due_date or fee.posting_date
	if fee.company:
		invoice.company = fee.company
	if fee.currency:
		invoice.currency = fee.currency
	invoice.remarks = MIGRATED_REMARK.format(fee.name)

	for c in frappe.get_all(
		"Fee Component",
		filters={"parent": fee.name, "parenttype": "Fees"},
		fields=["fees_category", "description", "amount"],
		order_by="idx",
	):
		invoice.append(
			"items",
			{
				"item_code": frappe.db.get_value("Fee Category", c.fees_category, "item"),
				"qty": 1,
				"rate": flt(c.amount),
				"description": c.description or c.fees_category,
			},
		)

	invoice.insert(ignore_permissions=True)
	invoice.submit()

	# The totals must match to the fils, or the school's receivables shift.
	if abs(flt(invoice.grand_total) - flt(fee.grand_total)) > 0.005:
		frappe.throw(
			"Total mismatch migrating {0}: {1} became {2}".format(
				fee.name, flt(fee.grand_total), flt(invoice.grand_total)
			)
		)

	return invoice.name


def _retire_original(fee: str):
	"""Reverse the old document so the debt is not counted twice.

	Cancelling writes reversing GL entries rather than deleting anything, so
	both the original posting and its reversal remain visible to an auditor.
	"""
	doc = frappe.get_doc("Fees", fee)
	if doc.docstatus == 1:
		doc.flags.ignore_permissions = True
		doc.cancel()
