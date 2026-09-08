# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Who has not come back — and what they still owe.

Students holding a submitted Program Enrollment in the previous academic year
with no submitted enrolment at all in the new one. The retention list the
office works through in August, with the family's phone number and the debt
attached, because the two conversations are the same conversation.

Fee figures read Sales Invoice, the v16 model.

The v15 version selected `Student.student_id`, a column v16 does not have —
the report died on `Unknown column`. The student's own record id is shown
instead, which is what `student_id` held anyway.
"""

import frappe
from frappe import _
from frappe.utils import flt

from match_schools import ms_fee_reporting as fr


def execute(filters=None):
	filters = frappe._dict(filters or {})

	data = get_data(filters)
	details = [d for d in data if not d.get("is_total")]

	return (
		get_columns(),
		data,
		None,
		get_chart(details),
		get_report_summary(details, filters),
	)


def get_columns():
	return [
		{"label": _("Student"), "fieldname": "student", "fieldtype": "Link",
		 "options": "Student", "width": 130},
		{"label": _("Student Name"), "fieldname": "student_name", "fieldtype": "Data",
		 "width": 210},
		{"label": _("ID Number"), "fieldname": "id_number", "fieldtype": "Data", "width": 120},
		{"label": _("Previous Program"), "fieldname": "program", "fieldtype": "Link",
		 "options": "Program", "width": 150},
		{"label": _("Previous Batch"), "fieldname": "student_batch_name", "fieldtype": "Link",
		 "options": "Student Batch Name", "width": 120},
		{"label": _("Category"), "fieldname": "student_category", "fieldtype": "Link",
		 "options": "Student Category", "width": 120},
		{"label": _("Enrollment Date"), "fieldname": "enrollment_date", "fieldtype": "Date",
		 "width": 120},
		{"label": _("Program Enrollment"), "fieldname": "program_enrollment",
		 "fieldtype": "Link", "options": "Program Enrollment", "width": 150},
		{"label": _("Guardian"), "fieldname": "guardian", "fieldtype": "Link",
		 "options": "Guardian", "width": 130},
		{"label": _("Guardian Name"), "fieldname": "guardian_name", "fieldtype": "Data",
		 "width": 170},
		{"label": _("Guardian Mobile"), "fieldname": "guardian_mobile", "fieldtype": "Data",
		 "width": 130},
		{"label": _("Student Mobile"), "fieldname": "student_mobile_number",
		 "fieldtype": "Data", "width": 130},
		{"label": _("Left On"), "fieldname": "date_of_leaving", "fieldtype": "Date",
		 "width": 110},
		{"label": _("Reason For Leaving"), "fieldname": "reason_for_leaving",
		 "fieldtype": "Data", "width": 180},
		{"label": _("Prev. Year Fees"), "fieldname": "prev_grand_total",
		 "fieldtype": "Currency", "options": "currency", "width": 130},
		{"label": _("Prev. Year Paid"), "fieldname": "prev_paid_amount",
		 "fieldtype": "Currency", "options": "currency", "width": 130},
		{"label": _("Prev. Year Outstanding"), "fieldname": "prev_outstanding_amount",
		 "fieldtype": "Currency", "options": "currency", "width": 165},
		{"label": _("Total Outstanding (All Years)"), "fieldname": "total_outstanding",
		 "fieldtype": "Currency", "options": "currency", "width": 195},
		{"label": _("Payment Status"), "fieldname": "payment_status", "fieldtype": "Data",
		 "width": 130},
		{"label": _("Student Status"), "fieldname": "student_status", "fieldtype": "Data",
		 "width": 120},
		fr.currency_column(),
	]


def get_data(filters):
	if not filters.from_academic_year:
		frappe.throw(_("Please select the Previous Academic Year"))
	if not filters.to_academic_year:
		frappe.throw(_("Please select the New Academic Year"))
	if filters.from_academic_year == filters.to_academic_year:
		frappe.throw(_("Previous and New Academic Year must be different"))

	students = get_unenrolled_students(filters)
	if not students:
		return []

	names = [s.student for s in students]
	prev_fees = get_fees_summary(names, filters.from_academic_year, filters.get("company"))
	total_outstanding = get_total_outstanding(names, filters.get("company"))
	guardians = get_primary_guardians(names)
	currency = fr.default_currency()

	for s in students:
		fee = prev_fees.get(s.student) or {}
		guardian = guardians.get(s.student) or {}

		s.prev_grand_total = flt(fee.get("grand_total"))
		s.prev_paid_amount = flt(fee.get("paid_amount"))
		s.prev_outstanding_amount = flt(fee.get("outstanding_amount"))
		s.total_outstanding = flt(total_outstanding.get(s.student))
		s.currency = fee.get("currency") or currency
		s.guardian = guardian.get("guardian")
		s.guardian_name = guardian.get("guardian_name")
		s.guardian_mobile = guardian.get("mobile_number")
		s.payment_status = get_payment_status(s)
		s.student_status = (
			_("Left") if s.date_of_leaving else (_("Active") if s.enabled else _("Disabled"))
		)

	data = apply_post_filters(students, filters)
	if data:
		data.append(build_total_row(data, currency))

	return data


def get_unenrolled_students(filters):
	"""Enrolled in the old year, absent from the new one.

	"Absent from the new year" means no submitted enrolment in *any*
	programme — a child who moved from Grade 5 to Grade 6 has not left, and a
	report that matched on programme too would have listed the entire school.
	"""
	conditions = ["pe.docstatus = 1", "pe.academic_year = %(from_academic_year)s"]
	params = {
		"from_academic_year": filters.from_academic_year,
		"to_academic_year": filters.to_academic_year,
	}

	for key, column in (
		("program", "pe.program"),
		("student_batch_name", "pe.student_batch_name"),
		("student_category", "pe.student_category"),
	):
		if filters.get(key):
			conditions.append(f"{column} = %({key})s")
			params[key] = filters[key]

	return frappe.db.sql(
		f"""
		SELECT
			s.name                   AS student,
			s.student_name           AS student_name,
			s.ms_id_number           AS id_number,
			s.student_mobile_number  AS student_mobile_number,
			s.enabled                AS enabled,
			s.date_of_leaving        AS date_of_leaving,
			s.reason_for_leaving     AS reason_for_leaving,
			pe.name                  AS program_enrollment,
			pe.program               AS program,
			pe.student_batch_name    AS student_batch_name,
			pe.student_category      AS student_category,
			pe.enrollment_date       AS enrollment_date
		  FROM `tabProgram Enrollment` pe
		 INNER JOIN `tabStudent` s ON s.name = pe.student
		 WHERE {fr.where(conditions)}
		   AND NOT EXISTS (
			SELECT 1 FROM `tabProgram Enrollment` pe2
			 WHERE pe2.student = pe.student
			   AND pe2.academic_year = %(to_academic_year)s
			   AND pe2.docstatus = 1
		   )
		   AND pe.name = (
			SELECT pe3.name FROM `tabProgram Enrollment` pe3
			 WHERE pe3.student = pe.student
			   AND pe3.academic_year = %(from_academic_year)s
			   AND pe3.docstatus = 1
			 ORDER BY pe3.enrollment_date DESC, pe3.creation DESC
			 LIMIT 1
		   )
		 ORDER BY pe.program, s.student_name
		""",
		params,
		as_dict=True,
	)


def get_fees_summary(students, academic_year, company=None):
	"""Billed, paid and outstanding for one academic year."""
	conditions = [
		"si.docstatus = 1",
		fr.SCHOOL_INVOICE,
		"si.ms_academic_year = %(academic_year)s",
		"si.student IN %(students)s",
	]
	params = {"academic_year": academic_year, "students": students}
	if company:
		conditions.append("si.company = %(company)s")
		params["company"] = company

	rows = frappe.db.sql(
		f"""
		SELECT si.student                     AS student,
		       MAX(si.currency)               AS currency,
		       SUM(si.grand_total)            AS grand_total,
		       SUM({fr.PAID_AMOUNT})          AS paid_amount,
		       SUM(si.outstanding_amount)     AS outstanding_amount
		  FROM `tabSales Invoice` si
		 WHERE {fr.where(conditions)}
		 GROUP BY si.student
		""",
		params,
		as_dict=True,
	)
	return {r.student: r for r in rows}


def get_total_outstanding(students, company=None):
	"""Outstanding across every year — this is where old debt surfaces."""
	conditions = ["si.docstatus = 1", fr.SCHOOL_INVOICE, "si.student IN %(students)s"]
	params = {"students": students}
	if company:
		conditions.append("si.company = %(company)s")
		params["company"] = company

	rows = frappe.db.sql(
		f"""
		SELECT si.student AS student, SUM(si.outstanding_amount) AS outstanding
		  FROM `tabSales Invoice` si
		 WHERE {fr.where(conditions)}
		 GROUP BY si.student
		""",
		params,
		as_dict=True,
	)
	return {r.student: r.outstanding for r in rows}


def get_primary_guardians(students):
	"""The first guardian listed on each student, for the follow-up call."""
	rows = frappe.db.sql(
		"""
		SELECT sg.parent      AS student,
		       sg.guardian    AS guardian,
		       sg.guardian_name AS guardian_name,
		       g.mobile_number AS mobile_number
		  FROM `tabStudent Guardian` sg
		  LEFT JOIN `tabGuardian` g ON g.name = sg.guardian
		 WHERE sg.parenttype = 'Student' AND sg.parent IN %(students)s
		 ORDER BY sg.parent, sg.idx
		""",
		{"students": students},
		as_dict=True,
	)

	guardians = {}
	for r in rows:
		guardians.setdefault(r.student, r)
	return guardians


def get_payment_status(row):
	total = flt(row.prev_grand_total)
	outstanding = flt(row.prev_outstanding_amount)

	if not total:
		return _("No Fees")
	if outstanding <= fr.EPSILON:
		return _("Fully Paid")
	if total - outstanding <= fr.EPSILON:
		return _("Unpaid")
	return _("Partially Paid")


def apply_post_filters(rows, filters):
	"""Filters that depend on the aggregated fee figures, so cannot be SQL."""
	payment_status = filters.get("payment_status") or "All"
	if payment_status != "All":
		wanted = _(payment_status)
		rows = [r for r in rows if r.payment_status == wanted]

	if filters.get("only_with_outstanding"):
		rows = [r for r in rows if flt(r.total_outstanding) > fr.EPSILON]

	if filters.get("exclude_left_students"):
		rows = [r for r in rows if not r.date_of_leaving]

	return rows


def build_total_row(rows, currency):
	return frappe._dict({
		"student_name": _("Total ({0} students)").format(len(rows)),
		"prev_grand_total": sum(flt(r.prev_grand_total) for r in rows),
		"prev_paid_amount": sum(flt(r.prev_paid_amount) for r in rows),
		"prev_outstanding_amount": sum(flt(r.prev_outstanding_amount) for r in rows),
		"total_outstanding": sum(flt(r.total_outstanding) for r in rows),
		"currency": currency,
		"is_total": 1,
	})


def get_chart(rows):
	counts = {}
	for r in rows:
		program = r.get("program") or _("Not Set")
		counts[program] = counts.get(program, 0) + 1

	top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
	return {
		"data": {
			"labels": [p for p, _count in top],
			"datasets": [{"name": _("Not Enrolled"), "values": [c for _p, c in top]}],
		},
		"type": "bar",
		"colors": ["#dc3545"],
	}


def get_report_summary(rows, filters):
	currency = fr.default_currency()
	with_debt = len([r for r in rows if flt(r.total_outstanding) > fr.EPSILON])
	left = len([r for r in rows if r.get("date_of_leaving")])
	outstanding = sum(flt(r.total_outstanding) for r in rows)

	return [
		{"label": _("Not Enrolled in {0}").format(filters.to_academic_year or "-"),
		 "value": len(rows), "datatype": "Int", "indicator": "Red"},
		{"label": _("With Outstanding"), "value": with_debt, "datatype": "Int",
		 "indicator": "Orange"},
		{"label": _("Marked as Left"), "value": left, "datatype": "Int", "indicator": "Grey"},
		{"label": _("Total Outstanding"), "value": outstanding, "datatype": "Currency",
		 "currency": currency, "indicator": "Red"},
	]
