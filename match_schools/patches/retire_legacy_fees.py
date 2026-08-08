"""Retire the legacy `Fees` receivables now that billing runs on Sales Invoice.

The app reads fees exclusively from Sales Invoice. Leaving submitted `Fees`
documents behind would double-count: their receivable sits in the ledger and
in the AR reports, while the screens show only invoices — so a school would
see one number in ERPNext and another in this app.

Cancelling reverses each one's GL effect rather than deleting it, so the
original posting and its reversal both remain visible to an auditor and
nothing is silently rewritten.

Deliberately conservative. A Fee is left alone, and reported, when:

  * a payment has been recorded against it — unwinding a settled receivable
    is an accountant's decision, not a script's, and
  * it is referenced by a Journal Entry, because cancelling would strand that
    entry.

Run `report()` first: it changes nothing and lists exactly what would happen.
"""

import frappe
from frappe.utils import flt


def execute():
	"""Not run automatically.

	Retiring receivables is a decision a school makes deliberately, so this
	patch is a no-op on migrate and has to be invoked explicitly.
	"""
	return


def report() -> dict:
	"""What retiring would do, without doing any of it."""
	rows = frappe.get_all(
		"Fees",
		filters={"docstatus": 1},
		fields=["name", "student", "grand_total", "outstanding_amount", "owner"],
	)

	retire, keep = [], []
	for fee in rows:
		problem = _blocked(fee)
		(keep if problem else retire).append((fee.name, problem or "would retire"))

	return {
		"retire": retire,
		"keep": keep,
		"outstanding_retired": sum(
			flt(f.outstanding_amount)
			for f in rows
			if not _blocked(f)
		),
	}


def _blocked(fee) -> str | None:
	paid = flt(fee.grand_total) - flt(fee.outstanding_amount)
	if paid > 0.005:
		return f"has {paid:g} paid — needs an accountant"

	linked = frappe.db.count(
		"Journal Entry Account",
		{"reference_type": "Fees", "reference_name": fee.name, "docstatus": 1},
	)
	if linked:
		return f"referenced by {linked} journal entr{'y' if linked == 1 else 'ies'}"

	return None


def retire(confirm: bool = False) -> dict:
	"""Cancel the unpaid legacy fees.

	`confirm` must be passed explicitly — this reverses ledger entries, and a
	default-on version of that is exactly the kind of thing that goes wrong
	unattended.
	"""
	if not confirm:
		return {"error": "call retire(confirm=True) to proceed", **report()}

	done, skipped = [], []
	for fee in frappe.get_all(
		"Fees",
		filters={"docstatus": 1},
		fields=["name", "grand_total", "outstanding_amount"],
	):
		problem = _blocked(fee)
		if problem:
			skipped.append((fee.name, problem))
			continue
		try:
			doc = frappe.get_doc("Fees", fee.name)
			doc.flags.ignore_permissions = True
			doc.cancel()
			frappe.db.commit()
			done.append(fee.name)
		except Exception as exc:
			frappe.db.rollback()
			skipped.append((fee.name, str(exc)[:120]))

	return {"retired": done, "skipped": skipped}
