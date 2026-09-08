# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Attendance: the daily marking grid plus reporting."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from match_schools.api import academic_context as ctx
from match_schools.api.utils import (
	apply_period,
	BACK_OFFICE,
	fail,
	instructor_groups,
	ms_endpoint,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

# "Excused" is an absence the school has accepted a reason for. It is a
# separate status rather than a flag on Absent because it is excluded from
# every count: a certificate must not show the pupil as absent that day.
# "Leave" is kept only so records written before this distinction still read.
STATUS_AR = {
	"Present": "حاضر",
	"Absent": "غائب",
	"Excused": "غائب بعذر",
	"Leave": "غائب بعذر",
}

# Statuses that do not count against a student anywhere.
EXCUSED = ("Excused", "Leave")


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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


def _term_of(date) -> str | None:
	"""The academic term containing this date, if the school defines one."""
	rows = frappe.get_all(
		"Academic Term",
		filters={"term_start_date": ["<=", date], "term_end_date": [">=", date]},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None


def _assert_group_access(student_group: str, persona: str):
	"""A teacher may only touch groups they are assigned to."""
	if persona in BACK_OFFICE:
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def mark_attendance(student_group: str, date: str, entries: str | list, persona: str = None):
	"""Save the attendance grid. `entries` is [{student, status}, ...]."""
	_assert_group_access(student_group, persona)
	rows = frappe.parse_json(entries) if isinstance(entries, str) else entries
	if not rows:
		return fail(message_en="No entries supplied.", message_ar="لم يتم إرسال أي سجلات.")

	date = getdate(date or today())

	# --- Guards ---------------------------------------------------------
	# Attendance is a legal record, so every one of these refuses the whole
	# batch rather than saving part of it.

	if date > getdate(today()):
		return fail(
			message_en="Attendance cannot be recorded for a future date.",
			message_ar="لا يمكن تسجيل الحضور لتاريخ مستقبلي.",
		)

	reason = ctx.holiday_reason(date)
	if reason:
		return fail(
			message_en=f"The school is closed on this date ({reason}).",
			message_ar=f"المدرسة مغلقة في هذا التاريخ — {reason}.",
		)

	# A closed term is read-only unless the school allows staff to reopen it.
	if not ctx.can_write(persona, None, _term_of(date)):
		return fail(
			message_en="This academic term is closed.",
			message_ar="الفصل الدراسي مغلق ولا يمكن التعديل عليه.",
		)

	# Unknown statuses used to be skipped silently, so a typo in one row saved
	# the rest and reported success. Now the batch is rejected and named.
	roster = set(
		frappe.get_all(
			"Student Group Student",
			filters={"parent": student_group, "parenttype": "Student Group"},
			pluck="student",
		)
	)
	bad_status, not_in_group, seen = [], [], set()
	for row in rows:
		student = row.get("student")
		status = row.get("status")
		if not student:
			continue
		if status not in STATUS_AR:
			bad_status.append(student)
		if roster and student not in roster:
			not_in_group.append(student)
		if student in seen:
			return fail(
				message_en="The same student appears twice in this sheet.",
				message_ar="تكرر الطالب نفسه أكثر من مرة في هذا الكشف.",
			)
		seen.add(student)

	if bad_status:
		return fail(
			message_en=f"Invalid status for {len(bad_status)} student(s).",
			message_ar=f"حالة غير صالحة لـ {len(bad_status)} طالب.",
		)
	if not_in_group:
		return fail(
			message_en=f"{len(not_in_group)} student(s) are not in this section.",
			message_ar=f"{len(not_in_group)} طالب غير مسجّل في هذه الشعبة.",
		)

	saved, updated, unchanged = 0, 0, 0

	for row in rows:
		student = row.get("student")
		status = row.get("status")
		if not student or status not in STATUS_AR:
			continue
		result = _write_one(student, student_group, date, status)
		if result == "created":
			saved += 1
		elif result == "updated":
			updated += 1
		else:
			unchanged += 1

	frappe.db.commit()
	touched = saved + updated
	return {
		"success": True,
		"data": {"created": saved, "updated": updated, "unchanged": unchanged},
		"message_en": f"Attendance saved for {touched} student(s).",
		"message_ar": (
			f"تم حفظ الحضور لـ {touched} طالباً."
			if touched
			else "لا تغييرات — الحضور محفوظ كما هو."
		),
	}


def _write_one(student: str, student_group: str, date, status: str) -> str:
	"""Record one pupil's attendance. Returns created / updated / unchanged.

	A record whose status already matches is left completely alone. This
	matters more than it sounds: an attendance record is submitted, so
	changing one means cancelling it and writing a new one. Rewriting the
	whole class to correct a single pupil left a cancelled document behind for
	every child in the room, every time — thirty cancellations to fix one.
	"""
	existing = frappe.db.get_value(
		"Student Attendance",
		{"student": student, "student_group": student_group, "date": date, "docstatus": ["<", 2]},
		["name", "docstatus", "status"],
		as_dict=True,
	)

	if existing and existing.status == status:
		return "unchanged"

	if existing:
		doc = frappe.get_doc("Student Attendance", existing.name)
		if doc.docstatus == 1:
			# A submitted record cannot be edited. Cancelling and replacing is
			# ERPNext's own correction path, and the cancelled document is the
			# audit trail of what the register said before.
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
		return "updated"

	doc = frappe.new_doc("Student Attendance")
	doc.update(
		{"student": student, "student_group": student_group, "date": date, "status": status}
	)
	doc.insert()
	doc.submit()
	return "created"


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def mark_one(
	student: str = None,
	student_group: str = None,
	date: str = None,
	status: str = None,
	persona: str = None,
):
	"""Correct one pupil, without rewriting the register for the whole class.

	Marking a class is a batch; fixing a mistake is not. Sending the whole
	sheet to change one child cancelled and re-created every other record in
	it, which is both slow and a lie about what happened.
	"""
	if not (student and student_group and status):
		return fail(
			message_en="A student, a class and a status are required.",
			message_ar="يجب تحديد الطالب والشعبة والحالة.",
		)
	if status not in STATUS_AR:
		return fail(message_en="Unknown status.", message_ar="حالة غير معروفة.")

	_assert_group_access(student_group, persona)
	date = getdate(date or today())

	# The same three refusals the batch makes, for the same reasons.
	if date > getdate(today()):
		return fail(
			message_en="Attendance cannot be recorded for a future date.",
			message_ar="لا يمكن تسجيل الحضور لتاريخ مستقبلي.",
		)
	reason = ctx.holiday_reason(date)
	if reason:
		return fail(
			message_en=f"The school is closed on this date ({reason}).",
			message_ar=f"المدرسة مغلقة في هذا التاريخ — {reason}.",
		)
	if not ctx.can_write(persona, None, _term_of(date)):
		return fail(
			message_en="This academic term is closed.",
			message_ar="الفصل الدراسي مغلق ولا يمكن التعديل عليه.",
		)

	if not frappe.db.exists(
		"Student Group Student",
		{"parent": student_group, "parenttype": "Student Group", "student": student},
	):
		return fail(
			message_en="That student is not in this section.",
			message_ar="هذا الطالب غير مسجّل في هذه الشعبة.",
		)

	result = _write_one(student, student_group, date, status)
	frappe.db.commit()

	name = frappe.db.get_value("Student", student, "student_name") or student
	return {
		"success": True,
		"data": {"student": student, "status": status, "result": result},
		"message_en": f"{name}: {status}.",
		"message_ar": (
			f"{name}: {STATUS_AR[status]}"
			if result != "unchanged"
			else f"{name}: لا تغيير"
		),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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

	if student and persona in (*BACK_OFFICE, ROLE_TEACHER):
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
	present = counts.get("Present", 0)
	excused = sum(counts.get(k, 0) for k in EXCUSED)
	absent = counts.get("Absent", 0)
	# An excused day is left out of the denominator entirely: the school has
	# accepted the reason, so it must not lower the attendance rate, and a
	# certificate must not describe the pupil as absent.
	total = present + absent
	recorded = total + excused

	daily = frappe.db.sql(
		f"""
		SELECT sa.date,
			COUNT(*) AS total,
			SUM(CASE WHEN sa.status = 'Present' THEN 1 ELSE 0 END) AS present,
			SUM(CASE WHEN sa.status = 'Absent' THEN 1 ELSE 0 END) AS absent,
			SUM(CASE WHEN sa.status IN ('Excused','Leave') THEN 1 ELSE 0 END) AS excused
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
			"absent": absent,
			"excused": excused,
			# Kept under the old key so nothing reading "leave" breaks.
			"leave": excused,
			"total": total,
			"recorded": recorded,
			"rate": round(flt(present) / flt(total) * 100, 1) if total else 0.0,
		},
		"rows": [
			{
				"date": str(r.date),
				"total": (r.present or 0) + (r.absent or 0),
				"present": r.present or 0,
				# Previously total - present, which counted an excused day as an
				# absence on every daily row.
				"absent": r.absent or 0,
				"excused": r.excused or 0,
				"rate": (
					round(flt(r.present) / flt((r.present or 0) + (r.absent or 0)) * 100, 1)
					if ((r.present or 0) + (r.absent or 0))
					else 0.0
				),
			}
			for r in daily
		],
		"chronic_absentees": _chronic_absentees(where, params)
		if persona in (*BACK_OFFICE, ROLE_TEACHER)
		else [],
	}


def _instructor_group_names(instructor: str | None) -> list[str]:
	"""موحَّدة الآن مع بقية الشاشات — انظر `instructor_groups`."""
	return instructor_groups(instructor)


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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def my_groups(persona: str = None):
	"""Groups the caller can mark attendance for."""
	if persona in BACK_OFFICE:
		groups = frappe.get_all(
			"Student Group",
			filters=apply_period({"disabled": 0}, "Student Group"),
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
