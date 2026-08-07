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

STATUS_AR = {"paid": "مدفوع", "partial": "جزئي", "late": "متأخر"}


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
	"""Fee invoices the caller may see."""
	scope = resolve_scope(persona)
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 25, 1), 100)

	filters = {"docstatus": 1}
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return {"items": [], "total": 0, "summary": _empty_summary()}
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view these fees."), frappe.PermissionError)
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
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total_count = frappe.db.count("Fees", filters)

	items = []
	for r in rows:
		paid = flt(r.grand_total) - flt(r.outstanding_amount)
		item_status = _status_of(r.grand_total, r.outstanding_amount)
		items.append(
			{
				"id": r.name,
				"student": r.student,
				"student_name": r.student_name,
				"grade": r.program,
				"date": str(r.posting_date or ""),
				"due_date": str(r.due_date or ""),
				"total": flt(r.grand_total),
				"paid": paid,
				"outstanding": flt(r.outstanding_amount),
				"status": item_status,
				"status_label": STATUS_AR[item_status],
				"term": r.academic_term,
				"year": r.academic_year,
				"currency": r.currency,
			}
		)

	if status and status != "all":
		items = [i for i in items if i["status"] == status]

	return {
		"items": items,
		"total": total_count,
		"page": page,
		"page_size": page_size,
		"summary": _summary(filters),
	}


def _empty_summary() -> dict:
	return {"total": 0.0, "collected": 0.0, "outstanding": 0.0, "collection_rate": 0.0}


def _summary(filters: dict) -> dict:
	"""Totals across everything matching the same filters."""
	conditions = ["docstatus = 1"]
	params: dict = {}

	student = filters.get("student")
	if isinstance(student, list) and student and student[0] == "in":
		conditions.append("student IN %(students)s")
		params["students"] = student[1]
	elif isinstance(student, str):
		conditions.append("student = %(student)s")
		params["student"] = student

	if filters.get("program"):
		conditions.append("program = %(program)s")
		params["program"] = filters["program"]

	row = frappe.db.sql(
		"""
		SELECT SUM(grand_total) AS total, SUM(outstanding_amount) AS outstanding
		FROM `tabFees`
		WHERE {conditions}
		""".format(conditions=" AND ".join(conditions)),
		params,
		as_dict=True,
	)
	total = flt(row[0].total) if row else 0.0
	outstanding = flt(row[0].outstanding) if row else 0.0
	collected = total - outstanding
	return {
		"total": total,
		"collected": collected,
		"outstanding": outstanding,
		"collection_rate": round(collected / total * 100, 1) if total else 0.0,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
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
	"""One invoice with its component breakdown."""
	scope = resolve_scope(persona)
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
