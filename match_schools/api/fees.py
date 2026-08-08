# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Fee invoices, payment status and collection reporting."""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	fail,
	ms_endpoint,
	resolve_scope,
	ROLE_SECRETARY,
)

STATUS_AR = {"paid": "مدفوع", "partial": "جزئي", "late": "متأخر", "draft": "مسودة"}


def _status_of(total: float, outstanding: float) -> str:
	paid = flt(total) - flt(outstanding)
	if flt(outstanding) <= 0:
		return "paid"
	if paid > flt(total) * 0.3:
		return "partial"
	return "late"


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def list_fees(
	student: str = None,
	program: str = None,
	status: str = None,
	page: int = 1,
	page_size: int = 25,
	persona: str = None,
):
	"""Fee invoices the caller may see, from both billing paths.

	v16 bills school fees as Sales Invoices, but invoices raised under the
	older `Fees` doctype are still owed and still have to be collected. Reading
	only one of the two would hide real money from the family and from the
	office, so this merges them and marks each row with its source.
	"""
	scope = resolve_scope(persona)
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 25, 1), 100)

	allowed = None
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return {
				"items": [],
				"total": 0,
				"page": page,
				"page_size": page_size,
				"summary": _empty_summary(),
			}
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view these fees."), frappe.PermissionError)

	rows = _legacy_fee_rows(student, program, allowed) + _sales_invoice_rows(
		student, program, allowed
	)

	# Newest first across both sources.
	rows.sort(key=lambda r: (r["date"] or "", r["id"]), reverse=True)

	if status and status != "all":
		rows = [r for r in rows if r["status"] == status]

	total_count = len(rows)
	start = (page - 1) * page_size
	items = rows[start : start + page_size]

	return {
		"items": items,
		"total": total_count,
		"page": page,
		"page_size": page_size,
		"summary": _summary_of(rows),
	}


def _row_common(
	*,
	source: str,
	name: str,
	student: str,
	student_name: str,
	program: str,
	posting_date,
	due_date,
	total,
	outstanding,
	term=None,
	year=None,
	currency=None,
	docstatus: int = 1,
) -> dict:
	paid = flt(total) - flt(outstanding)
	item_status = "draft" if cint(docstatus) == 0 else _status_of(total, outstanding)
	return {
		"id": name,
		"source": source,  # "invoice" (v16) or "fees" (legacy)
		"student": student,
		"student_name": student_name,
		"grade": program,
		"date": str(posting_date or ""),
		"due_date": str(due_date or ""),
		"total": flt(total),
		"paid": paid,
		"outstanding": flt(outstanding),
		"status": item_status,
		"status_label": STATUS_AR.get(item_status, item_status),
		"term": term,
		"year": year,
		"currency": currency,
		"docstatus": cint(docstatus),
	}


def _legacy_fee_rows(student, program, allowed) -> list[dict]:
	"""Submitted `Fees` documents — the pre-v16 path, still collectable."""
	filters = {"docstatus": 1}
	if allowed is not None:
		filters["student"] = student if student else ["in", allowed]
	elif student:
		filters["student"] = student
	if program:
		filters["program"] = program

	rows = frappe.get_all(
		"Fees",
		filters=filters,
		fields=[
			"name", "student", "student_name", "program", "posting_date",
			"due_date", "grand_total", "outstanding_amount", "academic_term",
			"academic_year", "currency",
		],
		order_by="posting_date desc",
		limit_page_length=0,
	)
	return [
		_row_common(
			source="fees",
			name=r.name,
			student=r.student,
			student_name=r.student_name,
			program=r.program,
			posting_date=r.posting_date,
			due_date=r.due_date,
			total=r.grand_total,
			outstanding=r.outstanding_amount,
			term=r.academic_term,
			year=r.academic_year,
			currency=r.currency,
		)
		for r in rows
	]


def _sales_invoice_rows(student, program, allowed) -> list[dict]:
	"""Sales Invoices raised for a student — the v16 path.

	Drafts are included for the office, because an unposted invoice is work in
	progress they need to see, but hidden from families: it is not yet a debt.
	"""
	filters: dict = {"student": ["!=", ""]}

	if allowed is not None:
		filters["student"] = student if student else ["in", allowed]
		filters["docstatus"] = 1
	else:
		if student:
			filters["student"] = student
		filters["docstatus"] = ["<", 2]

	if program:
		filters["ms_program"] = program

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name", "student", "customer", "posting_date", "due_date",
			"grand_total", "outstanding_amount", "docstatus", "currency",
			"ms_program", "ms_academic_year", "ms_academic_term",
		],
		order_by="posting_date desc",
		limit_page_length=0,
	)

	names = {r.student for r in rows if r.student}
	student_names = (
		dict(
			frappe.db.get_all(
				"Student",
				filters={"name": ["in", list(names)]},
				fields=["name", "student_name"],
				as_list=True,
			)
		)
		if names
		else {}
	)

	return [
		_row_common(
			source="invoice",
			name=r.name,
			student=r.student,
			student_name=student_names.get(r.student) or r.customer,
			program=r.ms_program,
			posting_date=r.posting_date,
			due_date=r.due_date,
			total=r.grand_total,
			outstanding=r.outstanding_amount,
			term=r.ms_academic_term,
			year=r.ms_academic_year,
			currency=r.currency,
			docstatus=r.docstatus,
		)
		for r in rows
	]


def _summary_of(rows: list[dict]) -> dict:
	"""Totals over the rows themselves, so both sources are counted once.

	Drafts are excluded: nothing is owed until an invoice is posted.
	"""
	posted = [r for r in rows if r["docstatus"] == 1]
	total = sum(flt(r["total"]) for r in posted)
	outstanding = sum(flt(r["outstanding"]) for r in posted)
	collected = total - outstanding
	return {
		"total": flt(total),
		"collected": flt(collected),
		"outstanding": flt(outstanding),
		"collection_rate": flt(collected / total * 100) if total else 0.0,
	}


def _empty_summary() -> dict:
	return {"total": 0.0, "collected": 0.0, "outstanding": 0.0, "collection_rate": 0.0}


def collection_report(months: int = 6, persona: str = None):
	"""Expected vs collected per month, for the finance chart."""
	months = min(max(cint(months) or 6, 1), 24)
	start = getdate(add_months(today(), -(months - 1))).replace(day=1)

	rows = frappe.db.sql(
		"""
		SELECT YEAR(posting_date) AS yr, MONTH(posting_date) AS mo,
			SUM(grand_total) AS expected,
			SUM(grand_total - outstanding_amount) AS collected
		FROM `tabFees`
		WHERE posting_date >= %(start)s AND docstatus = 1
		GROUP BY YEAR(posting_date), MONTH(posting_date)
		ORDER BY yr, mo
		""",
		{"start": start},
		as_dict=True,
	)

	from match_schools.api.dashboard import ARABIC_MONTHS

	return {
		"months": [
			{
				"month": ARABIC_MONTHS.get(r.mo, str(r.mo)),
				"expected": flt(r.expected),
				"collected": flt(r.collected),
			}
			for r in rows
		],
		"by_status": _status_breakdown(),
	}


def _status_breakdown() -> list[dict]:
	rows = frappe.db.sql(
		"""
		SELECT grand_total, outstanding_amount
		FROM `tabFees`
		WHERE docstatus = 1
		""",
		as_dict=True,
	)
	buckets = {"paid": 0, "partial": 0, "late": 0}
	for r in rows:
		buckets[_status_of(r.grand_total, r.outstanding_amount)] += 1
	return [
		{"status": key, "label": STATUS_AR[key], "count": value}
		for key, value in buckets.items()
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def fee_detail(fees: str, persona: str = None):
	"""One invoice with its line breakdown, from either billing path.

	The id alone says which: a Sales Invoice or a legacy `Fees` document. The
	shape returned is the same either way so the screen does not care.
	"""
	scope = resolve_scope(persona)

	if frappe.db.exists("Sales Invoice", fees):
		return _invoice_detail(fees, persona, scope)

	doc = frappe.db.get_value(
		"Fees",
		fees,
		[
			"name", "student", "student_name", "program", "posting_date", "due_date",
			"grand_total", "outstanding_amount", "academic_term", "academic_year", "currency",
		],
		as_dict=True,
	)
	if not doc:
		return fail(message_en="Invoice not found.", message_ar="لم يتم العثور على الفاتورة.")

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		if doc.student not in (scope.get("students") or []):
			frappe.throw(_("You are not allowed to view this invoice."), frappe.PermissionError)

	components = frappe.get_all(
		"Fee Component",
		filters={"parent": fees, "parenttype": "Fees"},
		fields=["fees_category", "description", "amount"],
		order_by="idx",
	)

	paid = flt(doc.grand_total) - flt(doc.outstanding_amount)
	status = _status_of(doc.grand_total, doc.outstanding_amount)
	return {
		"id": doc.name,
		"source": "fees",
		"student": doc.student,
		"student_name": doc.student_name,
		"grade": doc.program,
		"date": str(doc.posting_date or ""),
		"due_date": str(doc.due_date or ""),
		"total": flt(doc.grand_total),
		"paid": paid,
		"outstanding": flt(doc.outstanding_amount),
		"status": status,
		"status_label": STATUS_AR[status],
		"term": doc.academic_term,
		"year": doc.academic_year,
		"currency": doc.currency,
		"components": [
			{
				"category": c.fees_category,
				"description": c.description,
				"amount": flt(c.amount),
			}
			for c in components
		],
	}


def _invoice_detail(invoice: str, persona: str, scope: dict) -> dict:
	"""A Sales Invoice presented in the same shape as a legacy fee."""
	doc = frappe.get_doc("Sales Invoice", invoice)

	if not doc.get("student"):
		return fail(
			message_en="This invoice does not belong to a student.",
			message_ar="هذه الفاتورة غير مرتبطة بطالب.",
		)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		if doc.student not in (scope.get("students") or []):
			frappe.throw(_("You are not allowed to view this invoice."), frappe.PermissionError)
		if doc.docstatus != 1:
			# A draft is not yet a debt; families should not see one.
			return fail(
				message_en="Invoice not found.", message_ar="لم يتم العثور على الفاتورة."
			)

	paid = flt(doc.grand_total) - flt(doc.outstanding_amount)
	status = "draft" if doc.docstatus == 0 else _status_of(doc.grand_total, doc.outstanding_amount)
	return {
		"id": doc.name,
		"source": "invoice",
		"student": doc.student,
		"student_name": frappe.db.get_value("Student", doc.student, "student_name")
		or doc.customer,
		"grade": doc.get("ms_program"),
		"date": str(doc.posting_date or ""),
		"due_date": str(doc.due_date or ""),
		"total": flt(doc.grand_total),
		"paid": paid,
		"outstanding": flt(doc.outstanding_amount),
		"status": status,
		"status_label": STATUS_AR.get(status, status),
		"term": doc.get("ms_academic_term"),
		"year": doc.get("ms_academic_year"),
		"currency": doc.currency,
		"docstatus": doc.docstatus,
		"programEnrollment": doc.get("ms_program_enrollment"),
		"components": [
			{
				"category": i.item_code,
				"description": i.description,
				"amount": flt(i.amount),
			}
			for i in doc.items
		],
	}


# --- Write operations ------------------------------------------------------


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


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_fee(payload: str | dict, persona: str = None):
	"""Raise a fee invoice for a student, from a structure or free components."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	student = data.get("student")
	if not student or not frappe.db.exists("Student", student):
		return fail(message_en="Student not found.", message_ar="لم يتم العثور على الطالب.")

	# Fees requires a Program Enrollment and validates that it belongs to the
	# student, so resolve it rather than trusting the caller.
	enrollment = data.get("program_enrollment") or frappe.db.get_value(
		"Program Enrollment",
		{"student": student, "docstatus": ["<", 2]},
		"name",
		order_by="creation desc",
	)
	if not enrollment:
		return fail(
			message_en="This student has no program enrollment.",
			message_ar="لا يوجد تسجيل برنامج لهذا الطالب.",
		)

	components = data.get("components") or []
	structure = data.get("fee_structure")
	if not components and not structure:
		return fail(
			message_en="Choose a fee structure or add at least one component.",
			message_ar="اختر هيكل رسوم أو أضف بنداً واحداً على الأقل.",
		)

	fee_id = data.get("id") or data.get("name")
	if fee_id:
		doc = frappe.get_doc("Fees", fee_id)
		if doc.docstatus == 1:
			return fail(
				message_en="A submitted invoice cannot be edited.",
				message_ar="لا يمكن تعديل فاتورة معتمدة.",
			)
	else:
		doc = frappe.new_doc("Fees")

	enrollment_doc = frappe.db.get_value(
		"Program Enrollment",
		enrollment,
		["program", "academic_year", "academic_term", "student_batch_name"],
		as_dict=True,
	)

	doc.student = student
	doc.program_enrollment = enrollment
	doc.program = enrollment_doc.program
	doc.academic_year = data.get("academic_year") or enrollment_doc.academic_year
	doc.academic_term = data.get("academic_term") or enrollment_doc.academic_term
	doc.student_batch = enrollment_doc.student_batch_name
	doc.posting_date = data.get("posting_date") or today()
	doc.due_date = data.get("due_date") or today()
	if data.get("company"):
		doc.company = data["company"]

	if structure:
		doc.fee_structure = structure
		# Copy the structure's components, so the invoice stands on its own
		# even if the structure is edited later.
		if not components:
			components = [
				{"fees_category": c.fees_category, "amount": c.amount, "description": c.description}
				for c in frappe.get_doc("Fee Structure", structure).components
			]

	doc.set("components", [])
	for c in components:
		if not c.get("fees_category") or not flt(c.get("amount")):
			continue
		doc.append(
			"components",
			{
				"fees_category": c["fees_category"],
				"amount": flt(c["amount"]),
				"description": c.get("description"),
			},
		)

	if not doc.components:
		return fail(
			message_en="Every component needs a category and an amount.",
			message_ar="كل بند يحتاج فئة ومبلغاً.",
		)

	doc.save()
	if cint(data.get("submit", 1)):
		doc.submit()

	frappe.db.commit()
	return {
		"success": True,
		"data": {
			"id": doc.name,
			"student": doc.student,
			"grand_total": flt(doc.grand_total),
			"outstanding": flt(doc.outstanding_amount),
			"docstatus": doc.docstatus,
		},
		"message_en": "Invoice saved.",
		"message_ar": "تم حفظ الفاتورة.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def cancel_fee(fees: str, persona: str = None):
	"""Cancel an invoice, provided nothing has been paid against it."""
	doc = frappe.get_doc("Fees", fees)
	if doc.docstatus == 2:
		return fail(message_en="Already cancelled.", message_ar="الفاتورة ملغاة بالفعل.")
	if flt(doc.grand_total) - flt(doc.outstanding_amount) > 0:
		return fail(
			message_en="This invoice has payments against it; cancel those first.",
			message_ar="توجد دفعات على هذه الفاتورة، يجب إلغاؤها أولاً.",
		)
	doc.cancel()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Invoice cancelled.",
		"message_ar": "تم إلغاء الفاتورة.",
	}


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
	"""Take a payment against an invoice as a proper Payment Entry.

	Writing outstanding_amount directly would leave the ledger inconsistent,
	so this books a real Payment Entry against the receivable account.
	"""
	# A Sales Invoice is a normal sales document, so ERPNext's own
	# get_payment_entry handles it — the Journal Entry workaround below exists
	# only because a Payment Entry cannot reference a `Fees` document.
	if frappe.db.exists("Sales Invoice", fees):
		return _pay_sales_invoice(
			fees, amount, mode_of_payment, reference_no, reference_date, posting_date, remarks
		)

	doc = frappe.get_doc("Fees", fees)
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

	# A Payment Entry cannot reference a Fee: ERPNext's
	# get_valid_reference_doctypes() only allows sales documents for a Customer
	# party, and get_payment_entry() raises UnboundLocalError for "Fees"
	# outright. A Journal Entry does list Fees as a reference type, so the
	# receipt is booked that way and stays linked to the invoice.
	paid_to = _default_cash_account(doc.company, mode_of_payment)
	if not paid_to:
		return fail(
			message_en="No cash or bank account is configured for this company.",
			message_ar="لا يوجد حساب نقدي أو بنكي معرّف لهذه الشركة.",
		)

	customer = frappe.db.get_value("Student", doc.student, "customer")
	if not customer:
		return fail(
			message_en="This student has no customer record, so a payment cannot be booked.",
			message_ar="لا يوجد سجل عميل لهذا الطالب، تعذّر تسجيل الدفعة.",
		)

	entry = frappe.new_doc("Journal Entry")
	entry.voucher_type = "Journal Entry"
	entry.company = doc.company
	entry.posting_date = posting_date or today()
	entry.user_remark = remarks or _("Fee payment for {0}").format(doc.name)
	if mode_of_payment:
		entry.mode_of_payment = mode_of_payment
	if reference_no:
		entry.cheque_no = reference_no
		entry.cheque_date = reference_date or entry.posting_date

	# Money in: debit the cash/bank account.
	entry.append("accounts", {"account": paid_to, "debit_in_account_currency": amount})
	# Money owed: credit the receivable, cleared against this invoice.
	entry.append(
		"accounts",
		{
			"account": doc.receivable_account,
			"party_type": "Customer",
			"party": customer,
			"credit_in_account_currency": amount,
			"reference_type": "Fees",
			"reference_name": doc.name,
		},
	)

	entry.flags.ignore_permissions = True
	entry.insert()
	entry.submit()

	# Education never recalculates Fees.outstanding_amount after submit — it is
	# only set once, from grand_total. Derive it from the ledger so the invoice
	# agrees with the accounts.
	_sync_outstanding(doc.name)
	frappe.db.commit()

	doc.reload()
	return {
		"success": True,
		"data": {
			"journal_entry": entry.name,
			"fees": doc.name,
			"paid": amount,
			"outstanding": flt(doc.outstanding_amount),
			"status": _status_of(flt(doc.grand_total), flt(doc.outstanding_amount)),
		},
		"message_en": "Payment recorded.",
		"message_ar": "تم تسجيل الدفعة.",
	}


def _sync_outstanding(fees: str) -> float:
	"""Recompute an invoice's outstanding balance from its ledger entries."""
	total = flt(frappe.db.get_value("Fees", fees, "grand_total"))
	settled = (
		frappe.db.sql(
			"""
			SELECT COALESCE(SUM(credit - debit), 0)
			FROM `tabGL Entry`
			WHERE against_voucher_type = 'Fees'
			  AND against_voucher = %(fees)s
			  AND voucher_no != %(fees)s
			  AND is_cancelled = 0
			""",
			{"fees": fees},
		)[0][0]
		or 0
	)
	outstanding = max(total - flt(settled), 0.0)
	frappe.db.set_value("Fees", fees, "outstanding_amount", outstanding, update_modified=False)
	return outstanding


def _default_cash_account(company: str, mode_of_payment: str = None) -> str | None:
	"""Where a received payment lands: the mode's account, else cash, else bank."""
	if mode_of_payment:
		account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mode_of_payment, "company": company},
			"default_account",
		)
		if account:
			return account

	for field in ("default_cash_account", "default_bank_account"):
		account = frappe.db.get_value("Company", company, field)
		if account:
			return account

	return frappe.db.get_value(
		"Account",
		{"company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
		"name",
	)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def list_payments(fees: str, persona: str = None):
	"""Payments booked against one invoice.

	Receipts are Journal Entries (a Payment Entry cannot reference a Fee), so
	the history is read from the journal rows that point at this invoice.
	"""
	# A Sales Invoice is paid by Payment Entry, which references it directly.
	if frappe.db.exists("Sales Invoice", fees):
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

	rows = frappe.get_all(
		"Journal Entry Account",
		filters={
			"reference_type": "Fees",
			"reference_name": fees,
			"docstatus": 1,
		},
		fields=["parent", "credit_in_account_currency", "debit_in_account_currency"],
	)
	if not rows:
		return []

	entries = {
		e.name: e
		for e in frappe.get_all(
			"Journal Entry",
			filters={"name": ["in", [r.parent for r in rows]], "docstatus": 1},
			fields=["name", "posting_date", "mode_of_payment", "cheque_no", "user_remark"],
		)
	}

	out = []
	for r in rows:
		entry = entries.get(r.parent)
		if not entry:
			continue
		# A credit to the receivable is money received; a debit would be a refund.
		amount = flt(r.credit_in_account_currency) - flt(r.debit_in_account_currency)
		out.append(
			{
				"id": entry.name,
				"amount": amount,
				"date": str(entry.posting_date or ""),
				"mode": entry.mode_of_payment,
				"reference": entry.cheque_no,
				"remarks": entry.user_remark,
			}
		)
	out.sort(key=lambda p: p["date"], reverse=True)
	return out


def _pay_sales_invoice(
	invoice: str,
	amount: float,
	mode_of_payment: str = None,
	reference_no: str = None,
	reference_date: str = None,
	posting_date: str = None,
	remarks: str = None,
) -> dict:
	"""Take a payment against a Sales Invoice as a real Payment Entry.

	Uses ERPNext's own `get_payment_entry`, so the receivable is cleared, the
	invoice's outstanding is recalculated by the framework, and the payment
	shows up in the standard AR reports — none of which happens if the figure
	is written by hand.
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	doc = frappe.get_doc("Sales Invoice", invoice)
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

	entry = get_payment_entry("Sales Invoice", invoice, party_amount=amount)
	entry.posting_date = posting_date or today()
	entry.reference_no = reference_no or invoice
	entry.reference_date = reference_date or entry.posting_date
	if mode_of_payment:
		entry.mode_of_payment = mode_of_payment
		account = _default_cash_account(doc.company, mode_of_payment)
		if account:
			entry.paid_to = account
	if remarks:
		entry.remarks = remarks

	# get_payment_entry allocates the full outstanding by default.
	entry.paid_amount = amount
	entry.received_amount = amount
	for ref in entry.references:
		ref.allocated_amount = amount

	entry.insert(ignore_permissions=True)
	entry.submit()
	frappe.db.commit()

	doc.reload()
	return {
		"id": invoice,
		"payment": entry.name,
		"paid": flt(doc.grand_total) - flt(doc.outstanding_amount),
		"outstanding": flt(doc.outstanding_amount),
		"status": _status_of(doc.grand_total, doc.outstanding_amount),
	}
