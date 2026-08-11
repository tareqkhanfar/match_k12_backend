
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""One place that knows how a school fee is stored.

v16 bills fees as Sales Invoices against the student's Customer. Every screen,
report, alert and export reads through this module rather than querying the
doctype directly, so the shape of a fee is defined once — and a future change
lands in one file instead of eleven.

A school invoice is a Sales Invoice carrying a `student`. Everything else on
Sales Invoice is ordinary trade and is never returned here.
"""

import frappe
from frappe.utils import cint, flt, getdate, today

STATUS_AR = {"paid": "مدفوع", "partial": "جزئي", "late": "متأخر", "draft": "مسودة"}

# The fields every caller needs; kept in one list so a column added here shows
# up everywhere consistently.
FIELDS = [
	"name",
	"student",
	"customer",
	"posting_date",
	"due_date",
	"grand_total",
	"outstanding_amount",
	"docstatus",
	"currency",
	"company",
	"ms_program",
	"ms_academic_year",
	"ms_academic_term",
	"ms_program_enrollment",
]


def status_of(total, outstanding, docstatus=1) -> str:
	if cint(docstatus) == 0:
		return "draft"
	if cint(docstatus) == 2:
		return "cancelled"
	paid = flt(total) - flt(outstanding)
	if flt(outstanding) <= 0.005:
		return "paid"
	if paid > 0.005:
		return "partial"
	return "late"


def base_filters(
	student=None,
	students=None,
	program=None,
	academic_year=None,
	academic_term=None,
	date_from=None,
	date_to=None,
	due_from=None,
	due_to=None,
	include_drafts=False,
) -> dict:
	"""Filters selecting school invoices only.

	`include_drafts` is for the office: an unposted invoice is work in
	progress, not money owed, so families never see one.
	"""
	filters: dict = {"student": ["!=", ""]}

	if student:
		filters["student"] = student
	elif students is not None:
		filters["student"] = ["in", list(students)]

	filters["docstatus"] = ["<", 2] if include_drafts else 1

	if program:
		filters["ms_program"] = program
	if academic_year:
		filters["ms_academic_year"] = academic_year
	if academic_term:
		filters["ms_academic_term"] = academic_term

	# Date ranges are inclusive on both ends, which is what a user means by
	# "from 1 March to 31 March".
	if date_from and date_to:
		filters["posting_date"] = ["between", [date_from, date_to]]
	elif date_from:
		filters["posting_date"] = [">=", date_from]
	elif date_to:
		filters["posting_date"] = ["<=", date_to]

	if due_from and due_to:
		filters["due_date"] = ["between", [due_from, due_to]]
	elif due_from:
		filters["due_date"] = [">=", due_from]
	elif due_to:
		filters["due_date"] = ["<=", due_to]

	return filters


def fetch(filters: dict, order_by="posting_date desc, creation desc", limit=0) -> list[dict]:
	"""Raw invoice rows, with the student's display name resolved."""
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=FIELDS,
		order_by=order_by,
		limit_page_length=limit or 0,
	)
	return _with_student_names(rows)


def _with_student_names(rows: list) -> list[dict]:
	names = {r.student for r in rows if r.get("student")}
	lookup = {}
	if names:
		lookup = dict(
			frappe.get_all(
				"Student",
				filters={"name": ["in", list(names)]},
				fields=["name", "student_name"],
				as_list=True,
			)
		)
	out = []
	for r in rows:
		d = dict(r)
		d["student_name"] = lookup.get(r.get("student")) or r.get("customer")
		out.append(d)
	return out


def to_row(r: dict) -> dict:
	"""The shape every fee screen consumes."""
	total = flt(r.get("grand_total"))
	outstanding = flt(r.get("outstanding_amount"))
	state = status_of(total, outstanding, r.get("docstatus"))
	due = r.get("due_date")
	return {
		"id": r.get("name"),
		"student": r.get("student"),
		"student_name": r.get("student_name"),
		"grade": r.get("ms_program"),
		"date": str(r.get("posting_date") or ""),
		"due_date": str(due or ""),
		"total": total,
		"paid": total - outstanding,
		"outstanding": outstanding,
		"status": state,
		"status_label": STATUS_AR.get(state, state),
		"term": r.get("ms_academic_term"),
		"year": r.get("ms_academic_year"),
		"currency": r.get("currency"),
		"docstatus": cint(r.get("docstatus")),
		"enrollment": r.get("ms_program_enrollment"),
		"overdue": bool(outstanding > 0.005 and due and getdate(due) < getdate(today())),
	}


def rows(**kwargs) -> list[dict]:
	"""Fetch and shape in one step — what most callers want."""
	include_drafts = kwargs.pop("include_drafts", False)
	order_by = kwargs.pop("order_by", "posting_date desc, creation desc")
	limit = kwargs.pop("limit", 0)
	return [
		to_row(r)
		for r in fetch(
			base_filters(include_drafts=include_drafts, **kwargs),
			order_by=order_by,
			limit=limit,
		)
	]


def totals(**kwargs) -> dict:
	"""Money summary over posted invoices only.

	Aggregated in SQL rather than by loading rows, so a school with tens of
	thousands of invoices does not pay for the whole table to build a KPI.
	"""
	filters = base_filters(**kwargs)
	conditions = ["si.docstatus = 1", "IFNULL(si.student, '') != ''"]
	params: dict = {}

	student = filters.get("student")
	if isinstance(student, list) and student and student[0] == "in":
		if not student[1]:
			return empty_totals()
		conditions.append("si.student IN %(students)s")
		params["students"] = student[1]
	elif isinstance(student, str) and student:
		conditions.append("si.student = %(student)s")
		params["student"] = student

	for key, column in (
		("ms_program", "si.ms_program"),
		("ms_academic_year", "si.ms_academic_year"),
		("ms_academic_term", "si.ms_academic_term"),
	):
		if filters.get(key):
			conditions.append(f"{column} = %({key})s")
			params[key] = filters[key]

	# Date filters must apply here too, or the KPI cards would contradict the
	# rows shown underneath them.
	for key, column in (("posting_date", "si.posting_date"), ("due_date", "si.due_date")):
		clause = filters.get(key)
		if not clause:
			continue
		op, value = clause[0], clause[1]
		if op == "between":
			conditions.append(f"{column} BETWEEN %({key}_from)s AND %({key}_to)s")
			params[f"{key}_from"], params[f"{key}_to"] = value[0], value[1]
		else:
			conditions.append(f"{column} {op} %({key}_v)s")
			params[f"{key}_v"] = value

	row = frappe.db.sql(
		"""
		SELECT IFNULL(SUM(si.grand_total), 0)        AS total,
		       IFNULL(SUM(si.outstanding_amount), 0) AS outstanding,
		       COUNT(*)                              AS count
		  FROM `tabSales Invoice` si
		 WHERE {}
		""".format(" AND ".join(conditions)),
		params,
		as_dict=True,
	)[0]

	total = flt(row.total)
	outstanding = flt(row.outstanding)
	collected = total - outstanding
	return {
		# Rounded to two decimals: these are currency figures and a percentage,
		# and an unrounded float renders as 65.21739130434783 on the screen.
		"total": round(total, 2),
		"collected": round(collected, 2),
		"outstanding": round(outstanding, 2),
		"count": cint(row.count),
		"collection_rate": round(collected / total * 100, 2) if total else 0.0,
	}


def empty_totals() -> dict:
	return {
		"total": 0.0,
		"collected": 0.0,
		"outstanding": 0.0,
		"count": 0,
		"collection_rate": 0.0,
	}


def outstanding_for(students: list[str]) -> dict[str, float]:
	"""Outstanding per student — used by dashboards and the alert rules."""
	if not students:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT student, IFNULL(SUM(outstanding_amount), 0) AS due
		  FROM `tabSales Invoice`
		 WHERE docstatus = 1 AND student IN %(students)s
		 GROUP BY student
		""",
		{"students": list(students)},
		as_dict=True,
	)
	return {r.student: flt(r.due) for r in rows}


def overdue_days_for(students: list[str]) -> dict[str, int]:
	"""Days past the oldest unpaid due date, per student."""
	if not students:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT student, MIN(due_date) AS oldest
		  FROM `tabSales Invoice`
		 WHERE docstatus = 1
		   AND outstanding_amount > 0.005
		   AND due_date IS NOT NULL
		   AND student IN %(students)s
		 GROUP BY student
		""",
		{"students": list(students)},
		as_dict=True,
	)
	out = {}
	for r in rows:
		if r.oldest:
			delta = (getdate(today()) - getdate(r.oldest)).days
			out[r.student] = max(delta, 0)
	return out
