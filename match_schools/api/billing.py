
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Billing on the v16 model: a Sales Invoice per student.

ERPNext v16 moved school billing onto Sales Invoice. A Student carries a
Customer, a Fee Structure carries the priced components, and the invoice is
raised against that Customer with the structure's items — which is what makes
fees appear in the standard receivables, ageing and sales reports rather than
in a parallel ledger of their own.

The desk has no way to do this for one student: `Fees` ships read-only and
`in_create`, and Fee Schedule requires Student Groups, so it can only bill a
whole class. That gap is what this module fills.

The older `Fees` documents are not touched. They stay readable through
`api/fees.py`; this module is the path forward.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	fail,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)

STATUS_AR = {"paid": "مدفوع", "partial": "جزئي", "late": "متأخر", "draft": "مسودة"}


def _status_of(total: float, outstanding: float, docstatus: int = 1) -> str:
	if cint(docstatus) == 0:
		return "draft"
	paid = flt(total) - flt(outstanding)
	if flt(outstanding) <= 0.005:
		return "paid"
	if paid > 0:
		return "partial"
	return "late"


def _customer_of(student: str) -> str | None:
	"""The Customer a Student bills through, creating it if Education has not.

	Education creates one on first save, but a Student imported or created
	another way may not have one yet, and an invoice cannot be raised without.
	"""
	customer = frappe.db.get_value("Student", student, "customer")
	if customer and frappe.db.exists("Customer", customer):
		return customer

	doc = frappe.get_doc("Student", student)
	doc.set_missing_customer_details()
	frappe.db.commit()
	return frappe.db.get_value("Student", student, "customer")


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def structure_options(student: str = None, persona: str = None):
	"""Fee Structures, ordered so the ones fitting this student come first.

	A registrar billing a specific child should not have to hunt for the right
	grade's structure among all of them.
	"""
	program = None
	academic_year = None
	if student:
		enrollment = frappe.db.get_value(
			"Program Enrollment",
			{"student": student, "docstatus": 1},
			["program", "academic_year"],
			as_dict=True,
			order_by="creation desc",
		)
		if enrollment:
			program = enrollment.program
			academic_year = enrollment.academic_year

	rows = frappe.get_all(
		"Fee Structure",
		fields=["name", "program", "academic_year", "academic_term", "total_amount"],
		order_by="creation desc",
		limit=200,
	)

	def rank(row):
		# Same programme and year first, then same programme, then the rest.
		if program and row.program == program:
			return 0 if (academic_year and row.academic_year == academic_year) else 1
		return 2

	items = [
		{
			"id": r.name,
			"program": r.program,
			"academicYear": r.academic_year,
			"academicTerm": r.academic_term,
			"total": flt(r.total_amount),
			"suggested": rank(r) == 0,
		}
		for r in sorted(rows, key=rank)
	]

	return {
		"structures": items,
		"studentProgram": program,
		"studentAcademicYear": academic_year,
		"companies": frappe.get_all("Company", pluck="name", limit=20),
		"defaultCompany": frappe.defaults.get_defaults().get("company"),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def structure_preview(fee_structure: str, persona: str = None):
	"""The components that would be billed, so the total is seen before saving."""
	if not frappe.db.exists("Fee Structure", fee_structure):
		return fail("Fee structure not found", "لم يتم العثور على هيكل الرسوم")

	doc = frappe.get_doc("Fee Structure", fee_structure)
	return {
		"id": doc.name,
		"program": doc.program,
		"academicYear": doc.academic_year,
		"academicTerm": doc.academic_term,
		"total": flt(doc.total_amount),
		"components": [
			{
				"category": c.fees_category,
				"item": c.item,
				"description": c.description,
				"amount": flt(c.amount),
			}
			for c in doc.components
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def invoice_student(
	student: str,
	fee_structure: str = None,
	components: str | list = None,
	program_enrollment: str = None,
	posting_date: str = None,
	due_date: str = None,
	company: str = None,
	remarks: str = None,
	invoice: str = None,
	submit: int = 0,
	persona: str = None,
):
	"""Create or update one Sales Invoice for one student.

	Saved as a draft by default so the lines can be corrected — amounts
	changed, rows added or removed — before anything reaches the ledger.
	Pass `submit=1` to post it.

	Either priced from a Fee Structure, which is the normal case since a
	school prices per grade, or from components given directly. Once created
	the components are the invoice's own: editing the structure later does not
	change an invoice already raised.
	"""
	if not frappe.db.exists("Student", student):
		return fail("Student not found", "لم يتم العثور على الطالب")

	customer = _customer_of(student)
	if not customer:
		return fail(
			"This student has no linked customer",
			"لا يوجد عميل مرتبط بهذا الطالب — تعذّر إصدار الفاتورة",
		)

	enrollment = program_enrollment or _resolve_enrollment(student)
	if not enrollment:
		return fail(
			"This student has no submitted program enrollment",
			"لا يوجد تسجيل دراسي معتمد لهذا الطالب — سجّله في برنامج أولاً",
		)

	rows = parse_json_arg(components) or []
	if fee_structure:
		if not frappe.db.exists("Fee Structure", fee_structure):
			return fail("Fee structure not found", "لم يتم العثور على هيكل الرسوم")
		structure = frappe.get_doc("Fee Structure", fee_structure)
		if not rows:
			rows = [
				{
					"item": c.item,
					"category": c.fees_category,
					"description": c.description,
					"amount": flt(c.amount),
				}
				for c in structure.components
			]

	if not rows:
		return fail(
			"Choose a fee structure or add at least one line",
			"اختر هيكل رسوم أو أضف بنداً واحداً على الأقل",
		)

	if invoice:
		if not frappe.db.exists("Sales Invoice", invoice):
			return fail("Invoice not found", "لم يتم العثور على الفاتورة")
		doc = frappe.get_doc("Sales Invoice", invoice)
		if doc.docstatus != 0:
			return fail(
				"A submitted invoice cannot be edited; cancel and amend it instead",
				"لا يمكن تعديل فاتورة معتمدة — ألغِها وأصدر تعديلاً",
			)
		doc.set("items", [])
	else:
		doc = frappe.new_doc("Sales Invoice")

	doc.customer = customer
	doc.student = student  # the custom field Education adds
	doc.ms_program_enrollment = enrollment
	doc.posting_date = posting_date or today()
	doc.due_date = due_date or add_days(today(), 30)
	if remarks:
		doc.remarks = remarks
	if company:
		doc.company = company

	missing_items = []
	for row in rows:
		item = row.get("item") or frappe.db.get_value(
			"Fee Category", row.get("category"), "item"
		)
		if not item or not frappe.db.exists("Item", item):
			missing_items.append(row.get("category") or row.get("item"))
			continue
		doc.append(
			"items",
			{
				"item_code": item,
				"qty": flt(row.get("qty")) or 1,
				"rate": flt(row.get("amount")),
				"description": row.get("description") or row.get("category") or item,
			},
		)

	if missing_items:
		# A Fee Category with no Item cannot be sold; say which one rather than
		# letting ERPNext fail on a blank item_code.
		return fail(
			"These fee categories have no linked item: {0}".format(", ".join(missing_items)),
			"هذه الفئات لا يوجد لها صنف مرتبط: {0}".format("، ".join(missing_items)),
		)

	if not doc.items:
		return fail("Nothing to invoice", "لا توجد بنود للفوترة")

	doc.save(ignore_permissions=True)
	if cint(submit):
		doc.submit()
	frappe.db.commit()

	return _invoice_payload(doc)


def _invoice_payload(doc) -> dict:
	return {
		"id": doc.name,
		"student": doc.student,
		"customer": doc.customer,
		"programEnrollment": doc.get("ms_program_enrollment"),
		"program": doc.get("ms_program"),
		"academicYear": doc.get("ms_academic_year"),
		"academicTerm": doc.get("ms_academic_term"),
		"postingDate": str(doc.posting_date or ""),
		"dueDate": str(doc.due_date or ""),
		"total": flt(doc.grand_total),
		"outstanding": flt(doc.outstanding_amount),
		"docstatus": doc.docstatus,
		"isDraft": doc.docstatus == 0,
		"status": _status_of(doc.grand_total, doc.outstanding_amount, doc.docstatus),
		"items": [
			{
				"idx": i.idx,
				"item": i.item_code,
				"description": i.description,
				"qty": flt(i.qty),
				"rate": flt(i.rate),
				"amount": flt(i.amount),
			}
			for i in doc.items
		],
	}


def _resolve_enrollment(student: str) -> str | None:
	"""The student's current enrolment, when there is exactly one obvious choice."""
	rows = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": 1},
		pluck="name",
		order_by="creation desc",
	)
	return rows[0] if rows else None


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def list_invoices(
	student: str = None,
	status: str = None,
	search: str = None,
	page: int = 1,
	page_size: int = 20,
	persona: str = None,
):
	"""Sales Invoices raised for students, scoped to what the caller may see."""
	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 20, 1), 100)

	filters: dict = {"docstatus": ["<", 2]}

	scope = resolve_scope(persona)
	allowed = list(filter(None, [scope.get("student")] + list(scope.get("students") or [])))
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		if not allowed:
			return {"items": [], "total": 0, "page": page, "page_size": page_size}
		filters["student"] = ["in", allowed]
	elif student:
		filters["student"] = student
	else:
		# Back office: only invoices that belong to a student.
		filters["student"] = ["!=", ""]

	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [["name", "like", like], ["customer", "like", like]]

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"student",
			"customer",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"docstatus",
			"status",
		],
		order_by="posting_date desc, creation desc",
		start=(page - 1) * page_size,
		page_length=page_size,
	)

	items = []
	for r in rows:
		computed = _status_of(r.grand_total, r.outstanding_amount, r.docstatus)
		if status and status != "all" and computed != status:
			continue
		items.append(
			{
				"id": r.name,
				"student": r.student,
				"studentName": frappe.db.get_value("Student", r.student, "student_name")
				if r.student
				else r.customer,
				"customer": r.customer,
				"postingDate": str(r.posting_date or ""),
				"dueDate": str(r.due_date or ""),
				"total": flt(r.grand_total),
				"outstanding": flt(r.outstanding_amount),
				"paid": flt(r.grand_total) - flt(r.outstanding_amount),
				"status": computed,
				"statusLabel": STATUS_AR.get(computed, computed),
				"overdue": bool(
					r.outstanding_amount > 0 and r.due_date and getdate(r.due_date) < getdate()
				),
			}
		)

	return {
		"items": items,
		"total": frappe.db.count("Sales Invoice", filters),
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def get_invoice(invoice: str, persona: str = None):
	"""One invoice with its lines, for the edit screen."""
	if not frappe.db.exists("Sales Invoice", invoice):
		return fail("Invoice not found", "لم يتم العثور على الفاتورة")
	return _invoice_payload(frappe.get_doc("Sales Invoice", invoice))


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def submit_invoice(invoice: str, persona: str = None):
	"""Post a draft to the ledger."""
	if not frappe.db.exists("Sales Invoice", invoice):
		return fail("Invoice not found", "لم يتم العثور على الفاتورة")

	doc = frappe.get_doc("Sales Invoice", invoice)
	if doc.docstatus == 1:
		return fail("Already submitted", "الفاتورة معتمدة مسبقاً")
	if doc.docstatus == 2:
		return fail("This invoice is cancelled", "هذه الفاتورة ملغاة")

	doc.submit()
	frappe.db.commit()
	return _invoice_payload(doc)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def cancel_invoice(invoice: str, persona: str = None):
	"""Reverse a posted invoice.

	Cancelling writes reversing GL entries rather than deleting anything, so
	the audit trail stays intact — which is why a correction is cancel then
	amend, not an edit in place.
	"""
	if not frappe.db.exists("Sales Invoice", invoice):
		return fail("Invoice not found", "لم يتم العثور على الفاتورة")

	doc = frappe.get_doc("Sales Invoice", invoice)
	if doc.docstatus == 2:
		return fail("Already cancelled", "الفاتورة ملغاة مسبقاً")

	paid = flt(doc.grand_total) - flt(doc.outstanding_amount)
	if doc.docstatus == 1 and paid > 0.005:
		# Cancelling would strand the payment; the money has to be dealt with
		# first, and saying so is more useful than a foreign-key error.
		return fail(
			"This invoice has payments against it; cancel those first",
			"توجد دفعات مسجلة على هذه الفاتورة — ألغِ الدفعات أولاً",
		)

	if doc.docstatus == 0:
		frappe.delete_doc("Sales Invoice", invoice, ignore_permissions=True)
		frappe.db.commit()
		return {"id": invoice, "deleted": True}

	doc.cancel()
	frappe.db.commit()
	return _invoice_payload(doc)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def student_enrollments(student: str, persona: str = None):
	"""The enrolments an invoice for this student may be attached to."""
	rows = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": 1},
		fields=["name", "program", "academic_year", "academic_term", "enrollment_date"],
		order_by="creation desc",
	)
	return [
		{
			"id": r.name,
			"program": r.program,
			"academicYear": r.academic_year,
			"academicTerm": r.academic_term,
			"enrolledOn": str(r.enrollment_date or ""),
		}
		for r in rows
	]
