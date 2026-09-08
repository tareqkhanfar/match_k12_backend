# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Money in and out of the school's tills, day by day.

Payment Entries with the mode, the reference and the counterparty — what the
cashier reconciles against at close of day.

Two things it does that the plain Payment Entry list cannot:

  * it names the **student** a receipt belongs to. In v16 a fee receipt is
    booked against the student's Customer, so the Payment Entry itself shows a
    customer name and nothing else; the student is one hop away through the
    invoice the payment settles. That hop is made here, once, in SQL.
  * it can be narrowed to school collections only, so tuition income can be
    read apart from the school's ordinary trade.

Custom fields (`custom_department`, `custom_branch`) are picked up only when a
site actually has them, so the report works on a site that does not.
"""

import frappe
from frappe import _
from frappe.utils import flt

# Optional site-specific dimensions: shown as columns and offered as filters
# only where the column exists.
OPTIONAL_FIELDS = {
	"custom_department": {"label": "Department", "options": "Department"},
	"custom_branch": {"label": "Branch", "options": "Branch"},
}

DOCSTATUS_BY_STATUS = {"Draft": 0, "Submitted": 1, "Cancelled": 2}
STATUS_BY_DOCSTATUS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def available_optional_fields():
	return [f for f in OPTIONAL_FIELDS if frappe.db.has_column("Payment Entry", f)]


def execute(filters=None):
	filters = frappe._dict(filters or {})

	optional = available_optional_fields()
	data = get_data(filters, optional)

	return get_columns(optional), data, None, get_chart(data), get_report_summary(data)


def get_columns(optional):
	columns = [
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Payment Entry"), "fieldname": "name", "fieldtype": "Link",
		 "options": "Payment Entry", "width": 160},
		{"label": _("Payment Type"), "fieldname": "payment_type", "fieldtype": "Data",
		 "width": 110},
		{"label": _("Party Type"), "fieldname": "party_type", "fieldtype": "Data",
		 "width": 110},
		{"label": _("Party"), "fieldname": "party", "fieldtype": "Dynamic Link",
		 "options": "party_type", "width": 150},
		{"label": _("Party Name"), "fieldname": "party_name", "fieldtype": "Data",
		 "width": 170},
		{"label": _("Student"), "fieldname": "student", "fieldtype": "Link",
		 "options": "Student", "width": 130},
		{"label": _("Student Name"), "fieldname": "student_name", "fieldtype": "Data",
		 "width": 180},
		{"label": _("Mode of Payment"), "fieldname": "mode_of_payment", "fieldtype": "Link",
		 "options": "Mode of Payment", "width": 130},
		{"label": _("Paid Amount"), "fieldname": "paid_amount", "fieldtype": "Currency",
		 "options": "currency", "width": 130},
		{"label": _("Reference No"), "fieldname": "reference_no", "fieldtype": "Data",
		 "width": 130},
		{"label": _("Reference Date"), "fieldname": "reference_date", "fieldtype": "Date",
		 "width": 115},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link",
		 "options": "Company", "width": 130},
	]

	for fieldname in optional:
		meta = OPTIONAL_FIELDS[fieldname]
		columns.append({
			"label": _(meta["label"]),
			"fieldname": fieldname,
			"fieldtype": "Link",
			"options": meta["options"],
			"width": 120,
		})

	columns += [
		{"label": _("Cost Center"), "fieldname": "cost_center", "fieldtype": "Link",
		 "options": "Cost Center", "width": 130},
		{"label": _("Project"), "fieldname": "project", "fieldtype": "Link",
		 "options": "Project", "width": 120},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Remarks"), "fieldname": "remarks", "fieldtype": "Small Text",
		 "width": 220},
		{"fieldname": "currency", "fieldtype": "Link", "options": "Currency", "hidden": 1},
	]
	return columns


def get_conditions(filters, optional):
	conditions = ["pe.docstatus < 2"]
	params = {}

	simple = {
		"from_date": ("pe.posting_date >= %(from_date)s", None),
		"to_date": ("pe.posting_date <= %(to_date)s", None),
		"company": ("pe.company = %(company)s", None),
		"payment_type": ("pe.payment_type = %(payment_type)s", None),
		"mode_of_payment": ("pe.mode_of_payment = %(mode_of_payment)s", None),
		"party_type": ("pe.party_type = %(party_type)s", None),
		"party": ("pe.party = %(party)s", None),
		"cost_center": ("pe.cost_center = %(cost_center)s", None),
		"project": ("pe.project = %(project)s", None),
	}
	for key, (clause, _unused) in simple.items():
		if filters.get(key):
			conditions.append(clause)
			params[key] = filters[key]

	for fieldname in optional:
		if filters.get(fieldname):
			conditions.append(f"pe.{fieldname} = %({fieldname})s")
			params[fieldname] = filters[fieldname]

	# Parameterised rather than interpolated: the value comes from a Select,
	# but a report filter is user input and is treated as such.
	if filters.get("status") in DOCSTATUS_BY_STATUS:
		conditions.append("pe.docstatus = %(docstatus)s")
		params["docstatus"] = DOCSTATUS_BY_STATUS[filters["status"]]

	if filters.get("students_only"):
		conditions.append(SCHOOL_PAYMENT)

	return conditions, params


#: A payment is a school collection when it settles an invoice naming a
#: student, or is booked directly against a Student party (legacy `Fees`).
SCHOOL_PAYMENT = """
	(
		pe.party_type = 'Student'
		OR EXISTS (
			SELECT 1
			  FROM `tabPayment Entry Reference` per
			 INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
			 WHERE per.parent = pe.name
			   AND per.reference_doctype = 'Sales Invoice'
			   AND IFNULL(si.student, '') != ''
		)
	)
"""


def get_data(filters, optional):
	conditions, params = get_conditions(filters, optional)
	optional_select = "".join(f", pe.{f}" for f in optional)

	rows = frappe.db.sql(
		f"""
		SELECT
			pe.posting_date,
			pe.name,
			pe.payment_type,
			pe.party_type,
			pe.party,
			pe.party_name,
			pe.mode_of_payment,
			pe.paid_amount,
			pe.reference_no,
			pe.reference_date,
			pe.company,
			pe.cost_center,
			pe.project,
			pe.docstatus,
			pe.remarks,
			pe.paid_from_account_currency AS currency
			{optional_select}
		  FROM `tabPayment Entry` pe
		 WHERE {" AND ".join(conditions)}
		 ORDER BY pe.posting_date DESC, pe.creation DESC
		""",
		params,
		as_dict=True,
	)

	attach_students(rows)
	for row in rows:
		row.status = STATUS_BY_DOCSTATUS.get(row.docstatus)

	return rows


def attach_students(rows):
	"""Name the student behind each receipt.

	Resolved in one query for the whole page rather than per row: the lookup
	is two joins deep, and doing it per receipt turns a day's takings into
	hundreds of round trips.
	"""
	if not rows:
		return

	names = [r.name for r in rows]
	# One entry per payment: a Payment Entry has a single party, and each
	# student bills through their own Customer, so every invoice a receipt
	# settles belongs to the same student.
	students = {}

	# Payments that settle a school Sales Invoice.
	for r in frappe.db.sql(
		"""
		SELECT per.parent AS payment, si.student AS student, s.student_name AS student_name
		  FROM `tabPayment Entry Reference` per
		 INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
		  LEFT JOIN `tabStudent` s ON s.name = si.student
		 WHERE per.reference_doctype = 'Sales Invoice'
		   AND per.parent IN %(payments)s
		   AND IFNULL(si.student, '') != ''
		""",
		{"payments": names},
		as_dict=True,
	):
		students.setdefault(r.payment, r)

	for row in rows:
		hit = students.get(row.name)
		if hit:
			row.student = hit.student
			row.student_name = hit.student_name
		elif row.party_type == "Student":
			# Legacy `Fees` receipts sat directly on the Student party.
			row.student = row.party
			row.student_name = row.party_name
		else:
			row.student = None
			row.student_name = None


def get_chart(rows):
	if not rows:
		return None

	by_mode = {}
	for row in rows:
		# Cancelled entries are listed for completeness but did not collect
		# anything, so they must not inflate the chart.
		if row.docstatus != 1:
			continue
		mode = row.mode_of_payment or _("Not Set")
		by_mode[mode] = by_mode.get(mode, 0) + flt(row.paid_amount)

	if not by_mode:
		return None

	ordered = sorted(by_mode.items(), key=lambda kv: kv[1], reverse=True)
	return {
		"data": {
			"labels": [m for m, _amount in ordered],
			"datasets": [{"name": _("Amount"), "values": [a for _m, a in ordered]}],
		},
		"type": "bar",
		"colors": ["#28a745"],
	}


def get_report_summary(rows):
	currency = frappe.db.get_default("currency")
	submitted = [r for r in rows if r.docstatus == 1]
	received = sum(flt(r.paid_amount) for r in submitted if r.payment_type == "Receive")
	paid_out = sum(flt(r.paid_amount) for r in submitted if r.payment_type == "Pay")
	student_receipts = sum(
		flt(r.paid_amount) for r in submitted if r.payment_type == "Receive" and r.student
	)

	return [
		{"label": _("Received"), "value": received, "datatype": "Currency",
		 "currency": currency, "indicator": "Green"},
		{"label": _("Of Which Student Fees"), "value": student_receipts,
		 "datatype": "Currency", "currency": currency, "indicator": "Blue"},
		{"label": _("Paid Out"), "value": paid_out, "datatype": "Currency",
		 "currency": currency, "indicator": "Orange"},
		{"label": _("Entries"), "value": len(rows), "datatype": "Int", "indicator": "Grey"},
	]
