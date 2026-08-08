
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
	posting_date: str = None,
	due_date: str = None,
	company: str = None,
	submit: int = 1,
	persona: str = None,
):
	"""Raise one Sales Invoice for one student.

	Either from a Fee Structure — the normal case, since a school prices per
	grade — or from components passed directly for a one-off charge.
	"""
	if not frappe.db.exists("Student", student):
		return fail("Student not found", "لم يتم العثور على الطالب")

	customer = _customer_of(student)
	if not customer:
		return fail(
			"This student has no linked customer",
			"لا يوجد عميل مرتبط بهذا الطالب — تعذّر إصدار الفاتورة",
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

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	invoice.student = student  # the custom field Education adds
	invoice.posting_date = posting_date or today()
	invoice.due_date = due_date or add_days(today(), 30)
	if company:
		invoice.company = company

	missing_items = []
	for row in rows:
		item = row.get("item") or frappe.db.get_value(
			"Fee Category", row.get("category"), "item"
		)
		if not item or not frappe.db.exists("Item", item):
			missing_items.append(row.get("category") or row.get("item"))
			continue
		invoice.append(
			"items",
			{
				"item_code": item,
				"qty": 1,
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

	if not invoice.items:
		return fail("Nothing to invoice", "لا توجد بنود للفوترة")

	invoice.insert(ignore_permissions=True)
	if cint(submit):
		invoice.submit()
	frappe.db.commit()

	return {
		"id": invoice.name,
		"student": student,
		"customer": customer,
		"total": flt(invoice.grand_total),
		"outstanding": flt(invoice.outstanding_amount),
		"docstatus": invoice.docstatus,
		"status": _status_of(invoice.grand_total, invoice.outstanding_amount, invoice.docstatus),
	}


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
