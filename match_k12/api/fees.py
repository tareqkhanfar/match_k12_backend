# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Fee invoices, payment status and collection reporting."""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, today

from match_k12.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	fail,
	k12_endpoint,
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
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

	from match_k12.api.dashboard import ARABIC_MONTHS

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
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
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
