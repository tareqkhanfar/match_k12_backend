# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Fee invoices, payment status and collection reporting.

Every fee is a Sales Invoice raised against the student's Customer — the v16
model. Reads go through `api/receivables.py` so the shape of a fee is defined
in one place, and writes go through `api/billing.py` so the enrolment rules
are enforced on a single code path.
"""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, today

from match_schools.api import receivables as rec
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	fail,
	ms_endpoint,
	resolve_scope,
)

STATUS_AR = rec.STATUS_AR


def _visible_students(persona: str, scope: dict, student: str = None):
	"""Which students this caller may see fees for.

	Returns (student, students, blocked). `blocked` short-circuits the caller
	when a family has no children attached, so an empty scope never widens
	into "everyone".
	"""
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return None, [], True
		if student:
			if student not in allowed:
				frappe.throw(
					_("You are not allowed to view these fees."), frappe.PermissionError
				)
			return student, None, False
		return None, allowed, False
	return student, None, False


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def list_fees(
	student: str = None,
	program: str = None,
	status: str = None,
	academic_year: str = None,
	academic_term: str = None,
	date_from: str = None,
	date_to: str = None,
	due_from: str = None,
	due_to: str = None,
	page: int = 1,
	page_size: int = 25,
	persona: str = None,
):
	"""Fee invoices the caller may see, narrowed by the screen's filters."""
	scope = resolve_scope(persona)
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 25, 1), 100)

	one, many, blocked = _visible_students(persona, scope, student)
	if blocked:
		return {
			"items": [],
			"total": 0,
			"page": page,
			"page_size": page_size,
			"summary": rec.empty_totals(),
		}

	# Drafts are work in progress for the office; a family is not in debt
	# until the invoice is posted.
	office = persona in (ROLE_ADMIN, ROLE_SECRETARY)

	query = {
		"student": one,
		"students": many,
		"program": program,
		"academic_year": academic_year,
		"academic_term": academic_term,
		"date_from": date_from,
		"date_to": date_to,
		"due_from": due_from,
		"due_to": due_to,
	}

	items = rec.rows(include_drafts=office, **query)
	if status and status != "all":
		items = [i for i in items if i["status"] == status]

	total_count = len(items)
	start = (page - 1) * page_size

	return {
		"items": items[start : start + page_size],
		"total": total_count,
		"page": page,
		"page_size": page_size,
		# The summary follows the same filters, so the cards always describe
		# the rows on screen.
		"summary": rec.totals(**query),
		"filter_options": _filter_options() if office else None,
	}


def _filter_options() -> dict:
	"""The values worth filtering by, taken from the invoices that exist."""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT ms_program, ms_academic_year, ms_academic_term
		  FROM `tabSales Invoice`
		 WHERE docstatus < 2 AND IFNULL(student, '') != ''
		""",
		as_dict=True,
	)
	return {
		"programs": sorted({r.ms_program for r in rows if r.ms_program}),
		"academicYears": sorted({r.ms_academic_year for r in rows if r.ms_academic_year}),
		"academicTerms": sorted({r.ms_academic_term for r in rows if r.ms_academic_term}),
		"statuses": [
			{"value": k, "label": v} for k, v in rec.STATUS_AR.items()
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def collection_report(months: int = 6, persona: str = None):
	"""Billed against collected, month by month."""
	months = min(max(cint(months) or 6, 1), 24)
	start = getdate(add_months(today(), -(months - 1))).replace(day=1)

	rows = frappe.db.sql(
		"""
		SELECT DATE_FORMAT(posting_date, '%%Y-%%m')     AS month,
		       IFNULL(SUM(grand_total), 0)              AS billed,
		       IFNULL(SUM(outstanding_amount), 0)       AS outstanding
		  FROM `tabSales Invoice`
		 WHERE docstatus = 1
		   AND IFNULL(student, '') != ''
		   AND posting_date >= %(start)s
		 GROUP BY month
		 ORDER BY month
		""",
		{"start": start},
		as_dict=True,
	)

	return {
		"months": [
			{
				"month": r.month,
				"billed": flt(r.billed),
				"collected": flt(r.billed) - flt(r.outstanding),
				"outstanding": flt(r.outstanding),
			}
			for r in rows
		],
		"by_status": _status_breakdown(),
	}


def _status_breakdown() -> list[dict]:
	"""How many invoices sit in each payment state."""
	rows = frappe.db.sql(
		"""
		SELECT grand_total, outstanding_amount
		  FROM `tabSales Invoice`
		 WHERE docstatus = 1 AND IFNULL(student, '') != ''
		""",
		as_dict=True,
	)
	counts = {"paid": 0, "partial": 0, "late": 0}
	for r in rows:
		counts[rec.status_of(r.grand_total, r.outstanding_amount)] += 1
	return [{"status": k, "label": STATUS_AR[k], "count": v} for k, v in counts.items()]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def fee_detail(fees: str, persona: str = None):
	"""One invoice with its line breakdown."""
	scope = resolve_scope(persona)

	if not frappe.db.exists("Sales Invoice", fees):
		return fail(message_en="Invoice not found.", message_ar="لم يتم العثور على الفاتورة.")

	doc = frappe.get_doc("Sales Invoice", fees)
	if not doc.get("student"):
		return fail(
			message_en="This invoice does not belong to a student.",
			message_ar="هذه الفاتورة غير مرتبطة بطالب.",
		)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		if doc.student not in (scope.get("students") or []):
			frappe.throw(_("You are not allowed to view this invoice."), frappe.PermissionError)
		if doc.docstatus != 1:
			# A draft is not yet a debt.
			return fail(
				message_en="Invoice not found.", message_ar="لم يتم العثور على الفاتورة."
			)

	row = rec.to_row(
		{
			**{f: doc.get(f) for f in rec.FIELDS},
			"student_name": frappe.db.get_value("Student", doc.student, "student_name")
			or doc.customer,
		}
	)
	row["components"] = [
		{"category": i.item_code, "description": i.description, "amount": flt(i.amount)}
		for i in doc.items
	]
	return row


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def fee_form_options(persona: str = None):
	"""Everything the invoice form needs to populate its dropdowns."""
	return {
		"structures": [
			{
				"id": r.name,
				"name": r.name,
				"program": r.program,
				"academic_year": r.academic_year,
				"academic_term": r.academic_term,
				"total": flt(r.total_amount),
			}
			for r in frappe.get_all(
				"Fee Structure",
				fields=["name", "program", "academic_year", "academic_term", "total_amount"],
				order_by="creation desc",
				limit=100,
			)
		],
		"categories": frappe.get_all("Fee Category", pluck="name", limit=100),
		"modes": frappe.get_all(
			"Mode of Payment", filters={"enabled": 1}, pluck="name", limit=50
		),
		"companies": frappe.get_all("Company", pluck="name", limit=20),
	}


# --- Write operations ------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_fee(payload: str | dict, persona: str = None):
	"""Raise or edit a fee invoice.

	Delegates to `billing.invoice_student`, which owns invoice creation and
	the enrolment rules, so there is one code path rather than two that have
	to be kept in step.
	"""
	from match_schools.api.billing import invoice_student

	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	components = [
		{
			"item": c.get("item") or c.get("fees_category"),
			"category": c.get("fees_category") or c.get("category"),
			"amount": flt(c.get("amount")),
			"description": c.get("description"),
		}
		for c in (data.get("components") or [])
	]

	return invoice_student(
		student=data.get("student"),
		fee_structure=data.get("fee_structure"),
		components=components or None,
		program_enrollment=data.get("program_enrollment"),
		posting_date=data.get("posting_date"),
		due_date=data.get("due_date"),
		company=data.get("company"),
		invoice=data.get("id") or data.get("name"),
		submit=cint(data.get("submit", 1)),
		persona=persona,
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def cancel_fee(fees: str, persona: str = None):
	"""Reverse an invoice."""
	from match_schools.api.billing import cancel_invoice

	return cancel_invoice(invoice=fees, persona=persona)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def record_payment(
	fees: str,
	amount: float,
	mode_of_payment: str = None,
	reference_no: str = None,
	reference_date: str = None,
	posting_date: str = None,
	remarks: str = None,
	persona: str = None,
):
	"""Take a payment as a proper Payment Entry.

	Writing `outstanding_amount` directly would leave the ledger inconsistent,
	so ERPNext's own `get_payment_entry` books the receipt: the receivable is
	cleared, the outstanding is recalculated by the framework, and the payment
	shows up in the standard AR reports.
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if not frappe.db.exists("Sales Invoice", fees):
		return fail(message_en="Invoice not found.", message_ar="لم يتم العثور على الفاتورة.")

	doc = frappe.get_doc("Sales Invoice", fees)
	if doc.docstatus != 1:
		return fail(
			message_en="Only a submitted invoice can take a payment.",
			message_ar="لا يمكن تسجيل دفعة إلا على فاتورة معتمدة.",
		)

	amount = flt(amount)
	if amount <= 0:
		return fail(
			message_en="The amount must be greater than zero.",
			message_ar="يجب أن يكون المبلغ أكبر من صفر.",
		)
	if amount > flt(doc.outstanding_amount) + 0.005:
		return fail(
			message_en=f"The amount exceeds the outstanding {flt(doc.outstanding_amount):g}.",
			message_ar=f"المبلغ أكبر من المتبقي {flt(doc.outstanding_amount):g}.",
		)

	entry = get_payment_entry("Sales Invoice", fees, party_amount=amount)
	entry.posting_date = posting_date or today()
	entry.reference_no = reference_no or fees
	entry.reference_date = reference_date or entry.posting_date
	if mode_of_payment:
		entry.mode_of_payment = mode_of_payment
		account = _default_cash_account(doc.company, mode_of_payment)
		if account:
			entry.paid_to = account
	if remarks:
		entry.remarks = remarks

	# get_payment_entry allocates the whole outstanding by default; a partial
	# receipt has to be told what it is paying.
	entry.paid_amount = amount
	entry.received_amount = amount
	for ref in entry.references:
		ref.allocated_amount = amount

	entry.insert(ignore_permissions=True)
	entry.submit()
	frappe.db.commit()

	doc.reload()
	return {
		"id": fees,
		"payment": entry.name,
		"paid": flt(doc.grand_total) - flt(doc.outstanding_amount),
		"outstanding": flt(doc.outstanding_amount),
		"status": rec.status_of(doc.grand_total, doc.outstanding_amount),
	}


def _default_cash_account(company: str, mode_of_payment: str = None) -> str | None:
	"""The account money lands in, preferring the mode's own default."""
	if mode_of_payment:
		account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mode_of_payment, "company": company},
			"default_account",
		)
		if account:
			return account

	for fieldname in ("default_cash_account", "default_bank_account"):
		account = frappe.db.get_value("Company", company, fieldname)
		if account:
			return account

	return frappe.db.get_value(
		"Account",
		{"company": company, "account_type": ["in", ("Cash", "Bank")], "is_group": 0},
		"name",
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def list_payments(fees: str, persona: str = None):
	"""Payments booked against one invoice."""
	scope = resolve_scope(persona)

	student = frappe.db.get_value("Sales Invoice", fees, "student")
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		if not student or student not in (scope.get("students") or []):
			frappe.throw(_("You are not allowed to view this invoice."), frappe.PermissionError)

	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": fees, "docstatus": 1},
		fields=["parent", "allocated_amount"],
	)
	if not refs:
		return []

	entries = {
		e.name: e
		for e in frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", [r.parent for r in refs]], "docstatus": 1},
			fields=["name", "posting_date", "mode_of_payment", "reference_no", "remarks"],
		)
	}

	out = []
	for r in refs:
		entry = entries.get(r.parent)
		if not entry:
			continue
		out.append(
			{
				"id": entry.name,
				"amount": flt(r.allocated_amount),
				"date": str(entry.posting_date or ""),
				"mode": entry.mode_of_payment,
				"reference": entry.reference_no,
				"remarks": entry.remarks,
			}
		)
	return sorted(out, key=lambda x: x["date"], reverse=True)


# --- The family's statement of account ----------------------------------------


def _statement_students(persona: str, scope: dict, student: str | None) -> list[str]:
	"""The children whose money this caller may see.

	A guardian sees every child of theirs and nobody else's; a student sees
	themselves; the office sees the one student it asks about. An empty scope
	stays empty — it never widens into "everyone".
	"""
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = list(scope.get("students") or [])
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view these fees."), frappe.PermissionError)
		return allowed
	if not student:
		frappe.throw(_("Choose a student."))
	return [student]


def _child_summary(student: str, name: str, invoices: list, last_payment: str | None) -> dict:
	billed = sum(flt(i.grand_total) for i in invoices if flt(i.grand_total) > 0)
	credited = -sum(flt(i.grand_total) for i in invoices if flt(i.grand_total) < 0)
	outstanding = sum(flt(i.outstanding_amount) for i in invoices)
	now = getdate(today())
	overdue = sum(
		flt(i.outstanding_amount)
		for i in invoices
		if flt(i.outstanding_amount) > 0.005 and i.due_date and getdate(i.due_date) < now
	)
	return {
		"student": student,
		"student_name": name,
		"billed": round(billed, 2),
		"credited": round(credited, 2),
		"paid": round(billed - credited - outstanding, 2),
		"outstanding": round(outstanding, 2),
		"overdue": round(overdue, 2),
		"invoices": sum(1 for i in invoices if flt(i.grand_total) > 0),
		"last_payment": last_payment,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def statement(student: str = None, persona: str = None):
	"""كشف الحساب — every child's position, and one child's ledger.

	`children` and `overall` answer "how do we stand, across all of them";
	`ledger` answers "what happened on this child's account", in date order
	with a running balance: each invoice owed, each payment made, each credit.

	Only posted documents count: a draft invoice is the office's work in
	progress and a family is not in debt until it is posted. The balance is
	pinned to the invoices' own outstanding amounts — if something other than
	a payment moved them (a write-off, a journal entry), a closing adjustment
	says so, so the statement never ends on a number that disagrees with the
	rest of the app.
	"""
	scope = resolve_scope(persona)
	students = _statement_students(persona, scope, student)
	currency = frappe.db.get_default("currency")
	if not students:
		return {
			"currency": currency,
			"children": [],
			"overall": _child_summary("", "", [], None),
			"student": None,
			"ledger": [],
			"closing_balance": 0.0,
		}

	names = dict(
		frappe.get_all(
			"Student",
			filters={"name": ["in", students]},
			fields=["name", "student_name"],
			as_list=True,
		)
	)
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"student": ["in", students], "docstatus": 1},
		fields=[
			"name", "student", "posting_date", "due_date", "grand_total",
			"outstanding_amount", "currency", "is_return", "return_against",
			"ms_academic_year", "ms_academic_term", "ms_program",
		],
		order_by="posting_date asc, creation asc",
		limit_page_length=0,
	)
	if invoices:
		currency = invoices[0].currency or currency

	# Payments, as allocated against these invoices.
	refs = (
		frappe.get_all(
			"Payment Entry Reference",
			filters={
				"reference_doctype": "Sales Invoice",
				"reference_name": ["in", [i.name for i in invoices]],
				"docstatus": 1,
			},
			fields=["parent", "reference_name", "allocated_amount"],
			limit_page_length=0,
		)
		if invoices
		else []
	)
	entries = {
		e.name: e
		for e in (
			frappe.get_all(
				"Payment Entry",
				filters={"name": ["in", list({r.parent for r in refs})], "docstatus": 1},
				fields=["name", "posting_date", "mode_of_payment", "reference_no", "creation"],
				limit_page_length=0,
			)
			if refs
			else []
		)
	}
	student_of = {i.name: i.student for i in invoices}

	by_child: dict[str, list] = {s: [] for s in students}
	for i in invoices:
		by_child.setdefault(i.student, []).append(i)
	last_paid: dict[str, str] = {}
	for r in refs:
		entry = entries.get(r.parent)
		child = student_of.get(r.reference_name)
		if entry and child:
			day = str(entry.posting_date or "")
			if day > last_paid.get(child, ""):
				last_paid[child] = day

	children = [
		_child_summary(s, names.get(s, s), by_child.get(s, []), last_paid.get(s))
		for s in students
	]
	overall = _child_summary("", "", invoices, max(last_paid.values(), default=None))

	ledger: list[dict] = []
	closing = 0.0
	selected = student if student in students else (students[0] if len(students) == 1 else None)
	if selected:
		mine = by_child.get(selected, [])
		# Item names describe what an invoice was for better than its number.
		items: dict[str, list[str]] = {}
		if mine:
			for it in frappe.get_all(
				"Sales Invoice Item",
				filters={"parent": ["in", [i.name for i in mine]]},
				fields=["parent", "item_name"],
				order_by="idx asc",
				limit_page_length=0,
			):
				bucket = items.setdefault(it.parent, [])
				if it.item_name and it.item_name not in bucket:
					bucket.append(it.item_name)

		events: list[tuple] = []
		for i in mine:
			amount = flt(i.grand_total)
			label = "، ".join(items.get(i.name, [])[:3])
			# Term names often carry their year already ("2025-2026 (الفصل
			# الثاني)"); naming the year twice pushes the due date off the line.
			term, year = i.ms_academic_term or "", i.ms_academic_year or ""
			period = term if (term and year and year in term) else " — ".join(x for x in (term, year) if x)
			if amount >= 0:
				events.append((str(i.posting_date or ""), 0, {
					"date": str(i.posting_date or ""),
					"kind": "invoice",
					"reference": i.name,
					"description": label or "فاتورة رسوم",
					"period": period,
					"due_date": str(i.due_date or ""),
					"debit": round(amount, 2),
					"credit": 0.0,
				}))
			else:
				events.append((str(i.posting_date or ""), 1, {
					"date": str(i.posting_date or ""),
					"kind": "credit",
					"reference": i.name,
					"description": "إشعار دائن" + (f" على {i.return_against}" if i.return_against else ""),
					"period": period,
					"due_date": "",
					"debit": 0.0,
					"credit": round(-amount, 2),
				}))

		# One row per payment, summing what it paid across this child's invoices.
		paid_by_entry: dict[str, float] = {}
		for r in refs:
			if student_of.get(r.reference_name) == selected:
				paid_by_entry[r.parent] = paid_by_entry.get(r.parent, 0.0) + flt(r.allocated_amount)
		for name, amount in paid_by_entry.items():
			entry = entries.get(name)
			if not entry or amount <= 0:
				continue
			how = " — ".join(x for x in (entry.mode_of_payment, entry.reference_no) if x)
			events.append((str(entry.posting_date or ""), 2, {
				"date": str(entry.posting_date or ""),
				"kind": "payment",
				"reference": entry.name,
				"description": "دفعة" + (f" ({how})" if how else ""),
				"period": "",
				"due_date": "",
				"debit": 0.0,
				"credit": round(amount, 2),
			}))

		# Same day: what was owed first, then what was credited and paid.
		events.sort(key=lambda e: (e[0], e[1]))
		balance = 0.0
		for _, _, row in events:
			balance = round(balance + row["debit"] - row["credit"], 2)
			row["balance"] = balance
			ledger.append(row)

		actual = round(sum(flt(i.outstanding_amount) for i in mine), 2)
		if abs(actual - balance) > 0.009:
			diff = round(actual - balance, 2)
			balance = actual
			ledger.append({
				"date": today(),
				"kind": "adjustment",
				"reference": "",
				"description": "تسويات أخرى على الحساب",
				"period": "",
				"due_date": "",
				"debit": diff if diff > 0 else 0.0,
				"credit": -diff if diff < 0 else 0.0,
				"balance": balance,
			})
		closing = balance

	return {
		"currency": currency,
		"children": children,
		"overall": overall,
		"student": selected,
		"student_name": names.get(selected) if selected else None,
		"ledger": ledger,
		"closing_balance": round(closing, 2),
	}
