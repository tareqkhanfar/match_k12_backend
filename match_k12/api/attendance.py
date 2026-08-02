# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Attendance: the daily marking grid plus reporting."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from match_k12.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	k12_endpoint,
	resolve_scope,
)

STATUS_AR = {"Present": "حاضر", "Absent": "غائب", "Leave": "إجازة"}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def get_group_sheet(student_group: str, date: str = None, persona: str = None):
	"""Roster for one group on one date, with any attendance already marked."""
	date = date or today()
	_assert_group_access(student_group, persona)

	students = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name", "group_roll_number"],
		order_by="group_roll_number, student_name",
	)

	existing = {
		r.student: r
		for r in frappe.get_all(
			"Student Attendance",
			filters={"student_group": student_group, "date": date, "docstatus": ["<", 2]},
			fields=["name", "student", "status", "docstatus"],
		)
	}

	rows = []
	for s in students:
		record = existing.get(s.student)
		rows.append(
			{
				"student": s.student,
				"student_name": s.student_name,
				"roll_number": s.group_roll_number,
				"status": record.status if record else None,
				"status_label": STATUS_AR.get(record.status) if record else None,
				"attendance_id": record.name if record else None,
				"submitted": bool(record and record.docstatus == 1),
			}
		)

	return {
		"student_group": student_group,
		"date": str(date),
		"students": rows,
		"marked": len(existing),
		"total": len(rows),
	}


def _assert_group_access(student_group: str, persona: str):
	"""A teacher may only touch groups they are assigned to."""
	if persona == ROLE_ADMIN:
		return
	scope = resolve_scope(persona)
	instructor = scope.get("instructor")
	if not instructor:
		frappe.throw(_("No instructor is linked to your account."), frappe.PermissionError)
	assigned = frappe.db.exists(
		"Student Group Instructor",
		{"parent": student_group, "parenttype": "Student Group", "instructor": instructor},
	)
	if not assigned:
		frappe.throw(_("You are not assigned to this group."), frappe.PermissionError)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def mark_attendance(student_group: str, date: str, entries: str | list, persona: str = None):
	"""Save the attendance grid. `entries` is [{student, status}, ...]."""
	_assert_group_access(student_group, persona)
	rows = frappe.parse_json(entries) if isinstance(entries, str) else entries
	if not rows:
		return fail(message_en="No entries supplied.", message_ar="لم يتم إرسال أي سجلات.")

	date = getdate(date or today())
	saved, updated = 0, 0

	for row in rows:
		student = row.get("student")
		status = row.get("status")
		if not student or status not in STATUS_AR:
			continue

		existing = frappe.db.get_value(
			"Student Attendance",
			{"student": student, "student_group": student_group, "date": date, "docstatus": ["<", 2]},
			["name", "docstatus"],
			as_dict=True,
		)

		if existing:
			doc = frappe.get_doc("Student Attendance", existing.name)
			if doc.docstatus == 1:
				# Submitted records are immutable — cancel and replace.
				doc.cancel()
				doc = frappe.new_doc("Student Attendance")
				doc.update(
					{
						"student": student,
						"student_group": student_group,
						"date": date,
						"status": status,
					}
				)
				doc.insert()
				doc.submit()
			else:
				doc.status = status
				doc.save()
				doc.submit()
			updated += 1
		else:
			doc = frappe.new_doc("Student Attendance")
			doc.update(
				{"student": student, "student_group": student_group, "date": date, "status": status}
			)
			doc.insert()
			doc.submit()
			saved += 1

	frappe.db.commit()
	return {
		"success": True,
		"data": {"created": saved, "updated": updated},
		"message_en": f"Attendance saved for {saved + updated} students.",
		"message_ar": f"تم حفظ الحضور لـ {saved + updated} طالباً.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def attendance_report(
	student: str = None,
	student_group: str = None,
	from_date: str = None,
	to_date: str = None,
	persona: str = None,
):
	"""Attendance totals and daily rows, scoped to the caller."""
	scope = resolve_scope(persona)
	to_date = to_date or today()
	from_date = from_date or add_days(to_date, -30)

	conditions = ["sa.docstatus < 2", "sa.date BETWEEN %(from_date)s AND %(to_date)s"]
	params = {"from_date": from_date, "to_date": to_date}

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return {"summary": {}, "rows": [], "chronic_absentees": []}
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)
		conditions.append("sa.student IN %(allowed)s")
		params["allowed"] = [student] if student else allowed
	elif persona == ROLE_TEACHER and not student_group:
		groups = _instructor_group_names(scope.get("instructor"))
		if not groups:
			return {"summary": {}, "rows": [], "chronic_absentees": []}
		conditions.append("sa.student_group IN %(groups)s")
		params["groups"] = groups

	if student and persona in (ROLE_ADMIN, ROLE_TEACHER):
		conditions.append("sa.student = %(student)s")
		params["student"] = student
	if student_group:
		_assert_group_access(student_group, persona)
		conditions.append("sa.student_group = %(student_group)s")
		params["student_group"] = student_group

	where = " AND ".join(conditions)

	totals = frappe.db.sql(
		f"""
		SELECT sa.status, COUNT(*) AS count
		FROM `tabStudent Attendance` sa
		WHERE {where}
		GROUP BY sa.status
		""",
		params,
		as_dict=True,
	)
	counts = {r.status: r.count for r in totals}
	total = sum(counts.values())
	present = counts.get("Present", 0)

	daily = frappe.db.sql(
		f"""
		SELECT sa.date,
			COUNT(*) AS total,
			SUM(CASE WHEN sa.status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance` sa
		WHERE {where}
		GROUP BY sa.date
		ORDER BY sa.date
		""",
		params,
		as_dict=True,
	)

	return {
		"summary": {
			"present": present,
			"absent": counts.get("Absent", 0),
			"leave": counts.get("Leave", 0),
			"total": total,
			"rate": round(flt(present) / flt(total) * 100, 1) if total else 0.0,
		},
		"rows": [
			{
				"date": str(r.date),
				"total": r.total,
				"present": r.present,
				"absent": r.total - r.present,
				"rate": round(flt(r.present) / flt(r.total) * 100, 1) if r.total else 0.0,
			}
			for r in daily
		],
		"chronic_absentees": _chronic_absentees(where, params)
		if persona in (ROLE_ADMIN, ROLE_TEACHER)
		else [],
	}


def _instructor_group_names(instructor: str | None) -> list[str]:
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


def _chronic_absentees(where: str, params: dict, threshold: float = 80.0) -> list[dict]:
	"""Students whose attendance rate falls below the threshold."""
	rows = frappe.db.sql(
		f"""
		SELECT sa.student, sa.student_name,
			COUNT(*) AS total,
			SUM(CASE WHEN sa.status = 'Present' THEN 1 ELSE 0 END) AS present
		FROM `tabStudent Attendance` sa
		WHERE {where}
		GROUP BY sa.student, sa.student_name
		HAVING total >= 3
		""",
		params,
		as_dict=True,
	)
	out = []
	for r in rows:
		rate = round(flt(r.present) / flt(r.total) * 100, 1) if r.total else 0.0
		if rate < threshold:
			out.append(
				{
					"student": r.student,
					"student_name": r.student_name,
					"rate": rate,
					"absent": r.total - r.present,
					"total": r.total,
				}
			)
	return sorted(out, key=lambda x: x["rate"])[:20]


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_TEACHER)
def my_groups(persona: str = None):
	"""Groups the caller can mark attendance for."""
	if persona == ROLE_ADMIN:
		groups = frappe.get_all(
			"Student Group",
			filters={"disabled": 0},
			fields=["name", "student_group_name", "program", "batch", "academic_year"],
			order_by="student_group_name",
		)
	else:
		scope = resolve_scope(persona)
		names = _instructor_group_names(scope.get("instructor"))
		groups = (
			frappe.get_all(
				"Student Group",
				filters={"name": ["in", names], "disabled": 0},
				fields=["name", "student_group_name", "program", "batch", "academic_year"],
				order_by="student_group_name",
			)
			if names
			else []
		)

	for g in groups:
		g["students"] = frappe.db.count(
			"Student Group Student",
			{"parent": g["name"], "parenttype": "Student Group", "active": 1},
		)
	return groups
