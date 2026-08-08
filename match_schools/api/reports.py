# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Cross-cutting reports: academic, attendance and financial."""

import frappe
from frappe.utils import add_months, flt, getdate, today

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_TEACHER,
	get_default_academic_year,
	ms_endpoint,
	resolve_scope,
	ROLE_SECRETARY,
)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def overview(persona: str = None):
	"""Everything the reports screen needs in one round trip."""
	groups = _scope_groups(persona, resolve_scope(persona))

	# Call the plain builders, not the decorated endpoints, so we get raw
	# data instead of nested envelopes.
	return {
		"academic": _academic(groups),
		"attendance": _attendance(groups),
		"financial": _financial() if persona in BACK_OFFICE else None,
		"academic_year": get_default_academic_year(),
	}


def _scope_groups(persona: str, scope: dict) -> list[str] | None:
	"""None means unrestricted; a list restricts to those student groups."""
	if persona in BACK_OFFICE:
		return None
	instructor = scope.get("instructor")
	if not instructor:
		return []
	return [
		r.parent
		for r in frappe.get_all(
			"Student Group Instructor",
			filters={"instructor": instructor, "parenttype": "Student Group"},
			fields=["parent"],
		)
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def academic_report(persona: str = None):
	"""Average score per course and per program."""
	return _academic(_scope_groups(persona, resolve_scope(persona)))


def _academic(groups: list | None) -> dict:
	conditions = ["ar.docstatus = 1", "ar.maximum_score > 0"]
	params: dict = {}
	if groups is not None:
		if not groups:
			return {"by_subject": [], "by_grade": [], "top_students": []}
		conditions.append("ar.student_group IN %(groups)s")
		params["groups"] = groups

	where = " AND ".join(conditions)

	by_subject = frappe.db.sql(
		f"""
		SELECT ar.course AS subject,
			AVG(ar.total_score / ar.maximum_score * 100) AS average,
			COUNT(*) AS results
		FROM `tabAssessment Result` ar
		WHERE {where}
		GROUP BY ar.course
		ORDER BY average DESC
		""",
		params,
		as_dict=True,
	)

	by_grade = frappe.db.sql(
		f"""
		SELECT ar.program AS grade,
			AVG(ar.total_score / ar.maximum_score * 100) AS average,
			COUNT(DISTINCT ar.student) AS students
		FROM `tabAssessment Result` ar
		WHERE {where}
		GROUP BY ar.program
		ORDER BY average DESC
		""",
		params,
		as_dict=True,
	)

	top_students = frappe.db.sql(
		f"""
		SELECT ar.student, ar.student_name,
			AVG(ar.total_score / ar.maximum_score * 100) AS average,
			COUNT(*) AS results
		FROM `tabAssessment Result` ar
		WHERE {where}
		GROUP BY ar.student, ar.student_name
		HAVING results >= 2
		ORDER BY average DESC
		LIMIT 10
		""",
		params,
		as_dict=True,
	)

	return {
		"by_subject": [
			{"subject": r.subject, "average": round(flt(r.average), 1), "results": r.results}
			for r in by_subject
			if r.subject
		],
		"by_grade": [
			{"grade": r.grade, "average": round(flt(r.average), 1), "students": r.students}
			for r in by_grade
			if r.grade
		],
		"top_students": [
			{
				"student": r.student,
				"student_name": r.student_name,
				"average": round(flt(r.average), 1),
			}
			for r in top_students
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def attendance_summary(persona: str = None, months: int = 6):
	"""Monthly attendance rate plus the worst-attending groups."""
	return _attendance(_scope_groups(persona, resolve_scope(persona)), months)


def _attendance(groups: list | None, months: int = 6) -> dict:
	start = getdate(add_months(today(), -(months - 1))).replace(day=1)

	conditions = ["sa.docstatus < 2", "sa.date >= %(start)s"]
	params: dict = {"start": start}
	if groups is not None:
		if not groups:
			return {"monthly": [], "by_group": [], "overall": 0.0}
		conditions.append("sa.student_group IN %(groups)s")
		params["groups"] = groups

	where = " AND ".join(conditions)

	from match_schools.api.dashboard import ARABIC_MONTHS

	monthly = frappe.db.sql(
		f"""
		SELECT YEAR(sa.date) AS yr, MONTH(sa.date) AS mo,
			COUNT(*) AS total,
			SUM(CASE WHEN sa.status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance` sa
		WHERE {where}
		GROUP BY YEAR(sa.date), MONTH(sa.date)
		ORDER BY yr, mo
		""",
		params,
		as_dict=True,
	)

	by_group = frappe.db.sql(
		f"""
		SELECT sa.student_group AS student_group,
			COUNT(*) AS total,
			SUM(CASE WHEN sa.status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance` sa
		WHERE {where}
		GROUP BY sa.student_group
		ORDER BY sa.student_group
		""",
		params,
		as_dict=True,
	)

	total_all = sum(flt(r.total) for r in monthly)
	present_all = sum(flt(r.present) for r in monthly)

	return {
		"monthly": [
			{
				"month": ARABIC_MONTHS.get(r.mo, str(r.mo)),
				"rate": round(flt(r.present) / flt(r.total) * 100, 1) if r.total else 0.0,
				"present": r.present,
				"absent": r.total - r.present,
			}
			for r in monthly
		],
		"by_group": [
			{
				"student_group": r.student_group,
				"rate": round(flt(r.present) / flt(r.total) * 100, 1) if r.total else 0.0,
				"total": r.total,
			}
			for r in by_group
			if r.student_group
		],
		"overall": round(present_all / total_all * 100, 1) if total_all else 0.0,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def financial_summary(persona: str = None, months: int = 6):
	"""Collection trend, per-program totals and payment-status split."""
	return _financial(months)


def _financial(months: int = 6) -> dict:
	start = getdate(add_months(today(), -(months - 1))).replace(day=1)

	from match_schools.api.dashboard import ARABIC_MONTHS

	monthly = frappe.db.sql(
		"""
		SELECT YEAR(posting_date) AS yr, MONTH(posting_date) AS mo,
			SUM(grand_total) AS expected,
			SUM(grand_total - outstanding_amount) AS collected
		FROM `tabSales Invoice`
		WHERE posting_date >= %(start)s AND docstatus = 1
		  AND IFNULL(student, '') != ''
		GROUP BY YEAR(posting_date), MONTH(posting_date)
		ORDER BY yr, mo
		""",
		{"start": start},
		as_dict=True,
	)

	by_program = frappe.db.sql(
		"""
		SELECT ms_program AS program,
			SUM(grand_total) AS total,
			SUM(outstanding_amount) AS outstanding,
			COUNT(*) AS invoices
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND IFNULL(student, '') != ''
		GROUP BY ms_program
		ORDER BY total DESC
		""",
		as_dict=True,
	)

	totals = frappe.db.sql(
		"""
		SELECT SUM(grand_total) AS total, SUM(outstanding_amount) AS outstanding
		FROM `tabSales Invoice` WHERE docstatus = 1 AND IFNULL(student, '') != ''
		""",
		as_dict=True,
	)
	total = flt(totals[0].total) if totals else 0.0
	outstanding = flt(totals[0].outstanding) if totals else 0.0

	return {
		"monthly": [
			{
				"month": ARABIC_MONTHS.get(r.mo, str(r.mo)),
				"expected": flt(r.expected),
				"collected": flt(r.collected),
			}
			for r in monthly
		],
		"by_program": [
			{
				"program": r.program,
				"total": flt(r.total),
				"collected": flt(r.total) - flt(r.outstanding),
				"outstanding": flt(r.outstanding),
				"invoices": r.invoices,
			}
			for r in by_program
			if r.program
		],
		"totals": {
			"total": total,
			"collected": total - outstanding,
			"outstanding": outstanding,
			"collection_rate": round((total - outstanding) / total * 100, 1) if total else 0.0,
		},
	}
