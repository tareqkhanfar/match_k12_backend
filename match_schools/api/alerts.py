# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Rule-driven alerts, warnings and access blocking.

The administration writes rules — "overdue by more than 500", "attendance
below 80%", "three or more failing subjects" — and the engine evaluates every
student against them. A match raises a MS Student Alert, which becomes the
student's disciplinary/financial file, reaches the parent, and can escalate to
blocking access to parts of the system.

Every trigger is a small function returning {student: measured_value}, so
adding a new condition means adding one function, not touching the engine.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, now_datetime, today

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	paginate,
	parse_json_arg,
	resolve_scope,
)

SEVERITY_AR = {
	"Info": "معلومة",
	"Warning": "تنبيه",
	"Serious": "إنذار",
	"Critical": "إنذار نهائي",
}
SEVERITY_EMOJI = {"Info": "ℹ️", "Warning": "⚠️", "Serious": "🚨", "Critical": "⛔"}
SEVERITY_RANK = {"Info": 0, "Warning": 1, "Serious": 2, "Critical": 3}

LEVEL_AR = {
	"Notify": "تنبيه",
	"Warning": "إنذار أول",
	"Final Warning": "إنذار نهائي",
	"Block Access": "حجب الوصول",
}
STATUS_AR = {
	"Open": "مفتوح",
	"Acknowledged": "تم الاطلاع",
	"Escalated": "مُصعّد",
	"Resolved": "مُعالج",
	"Dismissed": "مُلغى",
}

# Every condition the administration can build a rule on. `unit` drives how the
# measured value is phrased back to a parent.
TRIGGERS = {
	"Fee Overdue Amount": {"label": "مبلغ رسوم متأخر", "unit": "", "group": "المالية"},
	"Fee Overdue Days": {"label": "تأخر سداد بالأيام", "unit": "يوم", "group": "المالية"},
	"Attendance Rate": {"label": "نسبة الحضور", "unit": "%", "group": "الحضور"},
	"Absence Count": {"label": "عدد أيام الغياب", "unit": "يوم", "group": "الحضور"},
	"Consecutive Absence": {"label": "غياب متتالٍ", "unit": "يوم", "group": "الحضور"},
	"Failing Subjects": {"label": "عدد المواد الراسب فيها", "unit": "مادة", "group": "الأكاديمي"},
	"Subject Below Mark": {"label": "مادة أقل من علامة", "unit": "مادة", "group": "الأكاديمي"},
	"Overall Average Below": {"label": "المعدل العام", "unit": "%", "group": "الأكاديمي"},
	"Behaviour Points Below": {"label": "رصيد نقاط السلوك", "unit": "نقطة", "group": "السلوك"},
	"Negative Behaviour Count": {"label": "عدد المخالفات السلوكية", "unit": "مخالفة", "group": "السلوك"},
	"Missing Assignments": {"label": "واجبات غير مُسلّمة", "unit": "واجب", "group": "الأكاديمي"},
	"Late Assignments": {"label": "واجبات مُسلّمة متأخرة", "unit": "واجب", "group": "الأكاديمي"},
	"Library Overdue": {"label": "كتب مكتبة متأخرة", "unit": "كتاب", "group": "خدمات"},
	"Warning Count": {"label": "عدد الإنذارات السابقة", "unit": "إنذار", "group": "السلوك"},
}

# Pages a rule may block. Anything not listed here cannot be blocked, so a
# misconfigured rule can never lock a student out of the whole system.
BLOCKABLE_PAGES = {
	"/app/record": "علامات الطلبة",
	"/app/finals": "العلامات النهائية",
	"/app/exams": "جدول الامتحانات",
	"/app/certificates": "الشهادات والوثائق",
	"/app/library": "المكتبة",
	"/app/activities": "الأنشطة والرحلات",
	"/app/quizzes": "الاختبارات الإلكترونية",
	"/app/resources": "مصادر المواد",
}

# The whole portal, not a list of pages. A school that suspends a student
# wants everything closed, and listing every route would miss the next one
# added. The alerts page itself is never blocked — a block nobody can read the
# reason for is a dead end, and the student has to be able to see what to fix.
BLOCK_EVERYTHING = "*"
ALWAYS_ALLOWED = ("/app/alerts", "/app")


def _compare(value: float, operator: str, threshold: float) -> bool:
	if operator == ">=":
		return value >= threshold
	if operator == ">":
		return value > threshold
	if operator == "<=":
		return value <= threshold
	if operator == "<":
		return value < threshold
	return abs(value - threshold) < 0.001


def _scope_students(rule) -> list[str]:
	"""Which students a rule applies to."""
	if rule.applies_to == "Student Group" and rule.student_group:
		return frappe.get_all(
			"Student Group Student",
			filters={
				"parent": rule.student_group,
				"parenttype": "Student Group",
				"active": 1,
			},
			pluck="student",
		)
	if rule.applies_to == "Program" and rule.program:
		return frappe.get_all(
			"Program Enrollment",
			filters={"program": rule.program, "docstatus": ["<", 2]},
			pluck="student",
		)
	return frappe.get_all("Student", filters={"enabled": 1}, pluck="name", limit=2000)


# --- Measurements ----------------------------------------------------------
# Each returns {student: value} for the students in scope. A student missing
# from the result simply has nothing to measure.


def _measure_fee_overdue_amount(students: list[str], rule) -> dict[str, float]:
	out: dict[str, float] = {}
	for f in frappe.get_all(
		"Sales Invoice",
		filters={
			"student": ["in", students],
			"outstanding_amount": [">", 0],
			"docstatus": 1,
			"due_date": ["<", today()],
		},
		fields=["student", "outstanding_amount"],
		limit=5000,
	):
		out[f.student] = out.get(f.student, 0) + flt(f.outstanding_amount)
	return out


def _measure_fee_overdue_days(students: list[str], rule) -> dict[str, float]:
	"""How long the oldest unpaid invoice has been overdue."""
	out: dict[str, float] = {}
	for f in frappe.get_all(
		"Sales Invoice",
		filters={
			"student": ["in", students],
			"outstanding_amount": [">", 0],
			"docstatus": 1,
			"due_date": ["<", today()],
		},
		fields=["student", "due_date"],
		limit=5000,
	):
		days = frappe.utils.date_diff(today(), f.due_date)
		out[f.student] = max(out.get(f.student, 0), days)
	return out


def _attendance_rows(students: list[str], rule):
	filters = {"student": ["in", students], "docstatus": 1}
	if cint(rule.within_days):
		filters["date"] = [">=", add_days(today(), -cint(rule.within_days))]
	return frappe.get_all(
		"Student Attendance",
		filters=filters,
		fields=["student", "status", "date"],
		order_by="student, date",
		limit=20000,
	)


def _measure_attendance_rate(students: list[str], rule) -> dict[str, float]:
	totals: dict[str, list] = {}
	for a in _attendance_rows(students, rule):
		totals.setdefault(a.student, []).append(a.status)
	return {
		s: round(sum(1 for x in rows if x == "Present") / len(rows) * 100, 1)
		for s, rows in totals.items()
		if rows
	}


def _measure_absence_count(students: list[str], rule) -> dict[str, float]:
	out: dict[str, float] = {}
	for a in _attendance_rows(students, rule):
		if a.status == "Absent":
			out[a.student] = out.get(a.student, 0) + 1
	return out


def _measure_consecutive_absence(students: list[str], rule) -> dict[str, float]:
	"""The longest run of consecutive absent days."""
	by_student: dict[str, list] = {}
	for a in _attendance_rows(students, rule):
		by_student.setdefault(a.student, []).append(a)

	out: dict[str, float] = {}
	for student, rows in by_student.items():
		longest = run = 0
		for r in rows:
			if r.status == "Absent":
				run += 1
				longest = max(longest, run)
			else:
				run = 0
		if longest:
			out[student] = longest
	return out


def _term_grades_by_student(students: list[str]) -> dict[str, list]:
	"""Each student's per-subject final marks, from the gradebook."""
	from match_schools.api.gradebook import _compute_subject_grade

	entries: dict[str, dict[str, list]] = {}
	for e in frappe.get_all(
		"MS Gradebook Entry",
		filters={"student": ["in", students]},
		fields=[
			"student", "course", "score", "max_score", "weight",
			"is_bonus", "component_type",
		],
		limit=20000,
	):
		entries.setdefault(e.student, {}).setdefault(e.course, []).append(e)

	return {
		student: [_compute_subject_grade(rows) for rows in courses.values()]
		for student, courses in entries.items()
	}


def _measure_failing_subjects(students: list[str], rule) -> dict[str, float]:
	pass_mark = flt(rule.threshold) if rule.trigger == "Subject Below Mark" else 50
	return {
		s: sum(1 for g in subjects if g["final"] < pass_mark)
		for s, subjects in _term_grades_by_student(students).items()
	}


def _measure_subject_below_mark(students: list[str], rule) -> dict[str, float]:
	"""Count of subjects under the rule's own mark — the threshold is the mark."""
	mark = flt(rule.threshold)
	return {
		s: sum(1 for g in subjects if g["final"] < mark)
		for s, subjects in _term_grades_by_student(students).items()
	}


def _measure_overall_average(students: list[str], rule) -> dict[str, float]:
	return {
		s: round(sum(g["final"] for g in subjects) / len(subjects), 1)
		for s, subjects in _term_grades_by_student(students).items()
		if subjects
	}


def _behaviour_rows(students: list[str], rule):
	filters = {"student": ["in", students]}
	if cint(rule.within_days):
		filters["record_date"] = [">=", add_days(today(), -cint(rule.within_days))]
	return frappe.get_all(
		"MS Behaviour Record",
		filters=filters,
		fields=["student", "record_type", "points"],
		limit=10000,
	)


def _measure_behaviour_points(students: list[str], rule) -> dict[str, float]:
	out: dict[str, float] = {}
	for b in _behaviour_rows(students, rule):
		out[b.student] = out.get(b.student, 0) + flt(b.points)
	return out


def _measure_negative_behaviour(students: list[str], rule) -> dict[str, float]:
	out: dict[str, float] = {}
	for b in _behaviour_rows(students, rule):
		if b.record_type == "Negative":
			out[b.student] = out.get(b.student, 0) + 1
	return out


def _measure_missing_assignments(students: list[str], rule) -> dict[str, float]:
	"""Assignments past their due date with nothing submitted."""
	groups = {
		r.student: r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students], "parenttype": "Student Group", "active": 1},
			fields=["student", "parent"],
		)
	}
	if not groups:
		return {}

	from match_schools.api.assignments import assignments_for_groups, handed_in_pairs

	due = {"due_date": ["<", today()]}
	if cint(rule.within_days):
		due = {"due_date": ["between", [add_days(today(), -cint(rule.within_days)), today()]]}

	assignments = assignments_for_groups(list(set(groups.values())), due)
	if not assignments:
		return {}

	# Every class each piece of homework was set for, so a pupil in the second
	# section is measured against it too.
	reach: dict[str, set[str]] = {}
	for row in frappe.get_all(
		"MS Assignment Group",
		filters={"parent": ["in", [a.name for a in assignments]], "parenttype": "MS Assignment"},
		fields=["parent", "student_group"],
		limit_page_length=0,
	):
		reach.setdefault(row.parent, set()).add(row.student_group)
	for a in assignments:
		reach.setdefault(a.name, set()).add(a.student_group)

	# "Handed in", not "has a row": a row exists from the moment the pupil
	# opens the work, and counting those would quietly silence this alert.
	submitted = handed_in_pairs([a.name for a in assignments])

	out: dict[str, float] = {}
	for student, group in groups.items():
		missing = sum(
			1
			for a in assignments
			if group in reach.get(a.name, set()) and (a.name, student) not in submitted
		)
		if missing:
			out[student] = missing
	return out


def _measure_late_assignments(students: list[str], rule) -> dict[str, float]:
	out: dict[str, float] = {}
	for s in frappe.get_all(
		"MS Assignment Submission",
		filters={"student": ["in", students], "status": "Late"},
		fields=["student"],
		limit=10000,
	):
		out[s.student] = out.get(s.student, 0) + 1
	return out


def _measure_library_overdue(students: list[str], rule) -> dict[str, float]:
	out: dict[str, float] = {}
	for loan in frappe.get_all(
		"MS Book Loan",
		filters={
			"student": ["in", students],
			"status": ["in", ["Issued", "Overdue"]],
			"due_date": ["<", today()],
		},
		fields=["student"],
		limit=5000,
	):
		out[loan.student] = out.get(loan.student, 0) + 1
	return out


def _measure_warning_count(students: list[str], rule) -> dict[str, float]:
	"""How many warnings a student already carries — for escalation rules."""
	out: dict[str, float] = {}
	for a in frappe.get_all(
		"MS Student Alert",
		filters={
			"student": ["in", students],
			"level": ["in", ["Warning", "Final Warning"]],
			"status": ["!=", "Dismissed"],
		},
		fields=["student"],
		limit=10000,
	):
		out[a.student] = out.get(a.student, 0) + 1
	return out


MEASURERS = {
	"Fee Overdue Amount": _measure_fee_overdue_amount,
	"Fee Overdue Days": _measure_fee_overdue_days,
	"Attendance Rate": _measure_attendance_rate,
	"Absence Count": _measure_absence_count,
	"Consecutive Absence": _measure_consecutive_absence,
	"Failing Subjects": _measure_failing_subjects,
	"Subject Below Mark": _measure_subject_below_mark,
	"Overall Average Below": _measure_overall_average,
	"Behaviour Points Below": _measure_behaviour_points,
	"Negative Behaviour Count": _measure_negative_behaviour,
	"Missing Assignments": _measure_missing_assignments,
	"Late Assignments": _measure_late_assignments,
	"Library Overdue": _measure_library_overdue,
	"Warning Count": _measure_warning_count,
}


# --- Evaluation ------------------------------------------------------------


# Money is the guardian's business, not the child's. A ten-year-old told the
# family owes 6,600 cannot act on it and should not be carrying it, so a fee
# alert reaches the guardian alone — and a block raised for unpaid fees closes
# the guardian's portal, never the student's schoolwork. Every other trigger
# (attendance, behaviour, marks) is about the student's own conduct and goes to
# both, which is what a school means by "notify the family".
FEE_TRIGGERS = {"Fee Overdue Amount", "Fee Overdue Days"}


def audience_for(trigger: str | None, roles: str | None = None) -> set[str]:
	"""Who may see an alert raised by this trigger.

	`roles` is the rule's own `notify_roles`, which until now was stored and
	never read: every alert reached everyone regardless of what the rule said.
	"""
	if trigger in FEE_TRIGGERS:
		return {ROLE_PARENT}
	if roles:
		chosen = {r.strip().lower() for r in str(roles).split(",") if r.strip()}
		allowed = chosen & {ROLE_STUDENT, ROLE_PARENT}
		if allowed:
			return allowed
	return {ROLE_STUDENT, ROLE_PARENT}


def _visible_to(persona: str, rows: list) -> list:
	"""Drop alerts this persona is not an audience for."""
	if persona not in (ROLE_STUDENT, ROLE_PARENT):
		return rows
	roles_by_rule: dict[str, str] = {}
	for rule in {r.get("rule") for r in rows if r.get("rule")}:
		actions = frappe.get_all(
			"MS Alert Action",
			filters={"parent": rule, "parenttype": "MS Alert Rule"},
			fields=["notify_roles"],
			limit_page_length=0,
		)
		joined = ",".join(a.notify_roles for a in actions if a.notify_roles)
		roles_by_rule[rule] = joined
	return [
		r
		for r in rows
		if persona in audience_for(r.get("trigger"), roles_by_rule.get(r.get("rule")))
	]


def _format_message(rule, value: float) -> tuple[str, str]:
	"""Fill the rule's own wording with the measured value.

	Placeholders {value} and {threshold} let the administration write the
	message once and have it read correctly for every student.
	"""
	unit = TRIGGERS.get(rule.trigger, {}).get("unit", "")
	if unit == "%":
		pretty = f"{value:g}%"
	elif unit:
		pretty = f"{value:g} {unit}"
	else:
		pretty = f"{value:g}"
	title = (rule.title_ar or "").replace("{value}", pretty).replace(
		"{threshold}", f"{flt(rule.threshold):g}"
	)
	message = (rule.message_ar or "").replace("{value}", pretty).replace(
		"{threshold}", f"{flt(rule.threshold):g}"
	)
	return title, message


def _level_for(rule, student: str) -> tuple[str, bool, list[str]]:
	"""Which action applies, given what the student already has.

	Actions are ordered, so a first offence gets the first action and a repeat
	escalates to the next one.
	"""
	actions = list(rule.actions_table)
	if not actions:
		return "Notify", False, []

	prior = frappe.db.count(
		"MS Student Alert",
		{"student": student, "rule": rule.name, "status": ["not in", ["Dismissed", "Resolved"]]},
	)
	action = actions[min(prior, len(actions) - 1)]

	pages = [
		p.strip()
		for p in (action.block_pages or "").split(",")
		if p.strip() in BLOCKABLE_PAGES or p.strip() == BLOCK_EVERYTHING
	]
	return action.action_type, action.action_type == "Block Access", pages


def evaluate_rule(rule) -> dict:
	"""Run one rule and raise alerts for whoever matches."""
	students = _scope_students(rule)
	if not students:
		return {"matched": 0, "raised": 0, "resolved": 0}

	measurer = MEASURERS.get(rule.trigger)
	if not measurer:
		return {"matched": 0, "raised": 0, "resolved": 0, "error": "unknown trigger"}

	measured = measurer(students, rule)
	matched = {
		s: v for s, v in measured.items() if _compare(flt(v), rule.operator, flt(rule.threshold))
	}

	raised = 0
	for student, value in matched.items():
		# One open alert per student per rule — re-running must not spam.
		existing = frappe.db.exists(
			"MS Student Alert",
			{"student": student, "rule": rule.name, "status": ["in", ["Open", "Acknowledged", "Escalated"]]},
		)
		if existing:
			frappe.db.set_value(
				"MS Student Alert", existing, "measured_value", flt(value), update_modified=False
			)
			continue

		level, blocks, pages = _level_for(rule, student)
		title, message = _format_message(rule, flt(value))

		doc = frappe.new_doc("MS Student Alert")
		doc.student = student
		doc.rule = rule.name
		doc.rule_name = rule.rule_name
		doc.trigger = rule.trigger
		doc.status = "Open"
		doc.severity = rule.severity
		doc.level = level
		doc.measured_value = flt(value)
		doc.threshold = flt(rule.threshold)
		doc.raised_on = now_datetime()
		# Anchored to the year and term it was raised in. Without them, last
		# year's warnings are indistinguishable from this year's on a student's
		# file, and a rule re-run in September reads as still-open history.
		doc.academic_year = rule.academic_year or get_default_academic_year()
		doc.academic_term = get_default_academic_term()
		doc.title_ar = title
		doc.message_ar = message
		doc.emoji = rule.emoji or SEVERITY_EMOJI.get(rule.severity, "⚠️")
		doc.blocks_access = cint(blocks)
		doc.resolution_notes = ",".join(pages) if pages else None
		doc.insert(ignore_permissions=True)
		raised += 1

	# A student who no longer matches has fixed the problem, so close it out
	# rather than leaving a stale warning on their file.
	resolved = 0
	for alert in frappe.get_all(
		"MS Student Alert",
		filters={"rule": rule.name, "status": ["in", ["Open", "Acknowledged", "Escalated"]]},
		fields=["name", "student"],
	):
		if alert.student not in matched:
			doc = frappe.get_doc("MS Student Alert", alert.name)
			doc.status = "Resolved"
			doc.resolved_on = now_datetime()
			doc.resolution_notes = "تمت المعالجة تلقائياً — لم تعد الحالة منطبقة"
			doc.save(ignore_permissions=True)
			resolved += 1

	frappe.db.set_value(
		"MS Alert Rule",
		rule.name,
		{"last_run_on": now_datetime(), "last_matched": len(matched)},
		update_modified=False,
	)

	return {"matched": len(matched), "raised": raised, "resolved": resolved}


def escalate_open_alerts() -> int:
	"""Move an unaddressed alert up to the rule's next action."""
	escalated = 0
	for alert in frappe.get_all(
		"MS Student Alert",
		# "Escalated" must be included, or an alert can only ever move up one
		# step and then sits at that level forever.
		filters={
			"status": ["in", ["Open", "Acknowledged", "Escalated"]],
			"rule": ["is", "set"],
		},
		fields=["name", "rule", "student", "raised_on", "escalated_on", "level"],
		limit=1000,
	):
		rule = frappe.get_doc("MS Alert Rule", alert.rule)
		actions = list(rule.actions_table)
		try:
			current = next(
				i for i, a in enumerate(actions) if a.action_type == alert.level
			)
		except StopIteration:
			continue
		if current + 1 >= len(actions):
			continue

		wait = cint(actions[current].escalate_after_days)
		if not wait:
			continue
		# Time runs from the last move, not from when the alert was first
		# raised, so each step waits its own interval.
		since = alert.escalated_on or alert.raised_on
		if frappe.utils.date_diff(today(), get_datetime(since).date()) < wait:
			continue

		nxt = actions[current + 1]
		pages = [
			p.strip()
			for p in (nxt.block_pages or "").split(",")
			if p.strip() in BLOCKABLE_PAGES or p.strip() == BLOCK_EVERYTHING
		]
		doc = frappe.get_doc("MS Student Alert", alert.name)
		doc.level = nxt.action_type
		doc.status = "Escalated"
		doc.escalated_on = now_datetime()
		doc.blocks_access = cint(nxt.action_type == "Block Access")
		if pages:
			doc.resolution_notes = ",".join(pages)
		doc.save(ignore_permissions=True)
		escalated += 1

	return escalated


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def run_rules(rule: str = None, persona: str = None):
	"""Evaluate one rule, or every enabled rule."""
	rules = (
		[frappe.get_doc("MS Alert Rule", rule)]
		if rule
		else [
			frappe.get_doc("MS Alert Rule", r.name)
			for r in frappe.get_all("MS Alert Rule", filters={"enabled": 1}, fields=["name"])
		]
	)
	if not rules:
		return fail(
			message_en="No enabled rules to run.",
			message_ar="لا توجد قواعد مفعّلة للتشغيل.",
		)

	totals = {"matched": 0, "raised": 0, "resolved": 0}
	per_rule = []
	for r in rules:
		result = evaluate_rule(r)
		for key in totals:
			totals[key] += result.get(key, 0)
		per_rule.append({"rule": r.name, "name": r.rule_name, **result})

	escalated = escalate_open_alerts()
	frappe.db.commit()

	return {
		"success": True,
		"data": {**totals, "escalated": escalated, "rules": per_rule},
		"message_en": f"Raised {totals['raised']}, resolved {totals['resolved']}.",
		"message_ar": (
			f"تم إصدار {totals['raised']} تنبيهاً، ومعالجة {totals['resolved']}"
			+ (f"، وتصعيد {escalated}." if escalated else ".")
		),
	}


def run_rules_scheduled():
	"""Hook target, so the rules run without anyone pressing a button."""
	for r in frappe.get_all("MS Alert Rule", filters={"enabled": 1}, fields=["name"]):
		try:
			evaluate_rule(frappe.get_doc("MS Alert Rule", r.name))
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Alert rule failed: {r.name}")
	escalate_open_alerts()
	frappe.db.commit()


# --- Rules API -------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def list_rules(persona: str = None):
	rows = frappe.get_all(
		"MS Alert Rule",
		fields=[
			"name", "rule_name", "trigger", "enabled", "severity", "operator",
			"threshold", "within_days", "applies_to", "program", "student_group",
			"title_ar", "message_ar", "emoji", "last_run_on", "last_matched",
		],
		order_by="modified desc",
		limit=200,
	)

	counts: dict[str, int] = {}
	for a in frappe.get_all(
		"MS Student Alert",
		filters={"status": ["in", ["Open", "Acknowledged", "Escalated"]]},
		fields=["rule"],
		limit=5000,
	):
		if a.rule:
			counts[a.rule] = counts.get(a.rule, 0) + 1

	return [
		{
			"id": r.name,
			"name": r.rule_name,
			"trigger": r.trigger,
			"trigger_label": TRIGGERS.get(r.trigger, {}).get("label", r.trigger),
			"group": TRIGGERS.get(r.trigger, {}).get("group", ""),
			"unit": TRIGGERS.get(r.trigger, {}).get("unit", ""),
			"enabled": bool(r.enabled),
			"severity": r.severity,
			"severity_label": SEVERITY_AR.get(r.severity, r.severity),
			"operator": r.operator,
			"threshold": flt(r.threshold),
			"within_days": cint(r.within_days),
			"applies_to": r.applies_to,
			"program": r.program,
			"student_group": r.student_group,
			"title": r.title_ar,
			"message": r.message_ar,
			"emoji": r.emoji,
			"last_run": str(r.last_run_on or ""),
			"last_matched": cint(r.last_matched),
			"open_alerts": counts.get(r.name, 0),
		}
		for r in rows
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def get_rule(rule: str, persona: str = None):
	doc = frappe.get_doc("MS Alert Rule", rule)
	return {
		"id": doc.name,
		"name": doc.rule_name,
		"trigger": doc.trigger,
		"enabled": bool(doc.enabled),
		"severity": doc.severity,
		"operator": doc.operator,
		"threshold": flt(doc.threshold),
		"within_days": cint(doc.within_days),
		"applies_to": doc.applies_to,
		"program": doc.program,
		"student_group": doc.student_group,
		"title": doc.title_ar,
		"message": doc.message_ar,
		"emoji": doc.emoji,
		"notes": doc.notes,
		"actions": [
			{
				"action_type": a.action_type,
				"action_label": LEVEL_AR.get(a.action_type, a.action_type),
				"notify_roles": a.notify_roles,
				"escalate_after_days": cint(a.escalate_after_days),
				"block_pages": a.block_pages,
			}
			for a in doc.actions_table
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_rule(payload: str | dict, persona: str = None):
	data = parse_json_arg(payload) or {}
	for field in ("rule_name", "trigger", "title_ar", "message_ar"):
		key = {"title_ar": "title", "message_ar": "message"}.get(field, field)
		if not data.get(key) and not data.get(field):
			return fail(
				message_en=f"{field} is required.",
				message_ar="اسم القاعدة والشرط ونص الرسالة مطلوبة.",
			)
	if data["trigger"] not in TRIGGERS:
		return fail(message_en="Unknown trigger.", message_ar="شرط غير معروف.")

	doc = (
		frappe.get_doc("MS Alert Rule", data["id"])
		if data.get("id")
		else frappe.new_doc("MS Alert Rule")
	)

	doc.rule_name = data.get("rule_name") or data.get("name")
	doc.trigger = data["trigger"]
	doc.enabled = cint(data.get("enabled", 1))
	doc.severity = data.get("severity") or "Warning"
	doc.operator = data.get("operator") or ">="
	doc.threshold = flt(data.get("threshold"))
	doc.within_days = cint(data.get("within_days"))
	doc.applies_to = data.get("applies_to") or "All"
	doc.program = data.get("program")
	doc.student_group = data.get("student_group")
	doc.title_ar = data.get("title") or data.get("title_ar")
	doc.message_ar = data.get("message") or data.get("message_ar")
	doc.emoji = data.get("emoji") or SEVERITY_EMOJI.get(doc.severity, "⚠️")
	doc.notes = data.get("notes")
	doc.academic_year = data.get("academic_year") or get_default_academic_year()

	if data.get("actions") is not None:
		doc.set("actions_table", [])
		for a in data["actions"]:
			pages = a.get("block_pages")
			if isinstance(pages, list):
				pages = ",".join(pages)
			doc.append(
				"actions_table",
				{
					"action_type": a.get("action_type") or "Notify",
					"notify_roles": a.get("notify_roles") or "student,parent",
					"escalate_after_days": cint(a.get("escalate_after_days")),
					"block_pages": pages,
				},
			)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "name": doc.rule_name},
		"message_en": "Rule saved.",
		"message_ar": "تم حفظ القاعدة.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_rule(rule: str, persona: str = None):
	open_alerts = frappe.db.count(
		"MS Student Alert",
		{"rule": rule, "status": ["in", ["Open", "Acknowledged", "Escalated"]]},
	)
	if open_alerts:
		return fail(
			message_en=f"{open_alerts} alert(s) are still open; disable the rule instead.",
			message_ar=f"يوجد {open_alerts} تنبيهاً مفتوحاً — عطّل القاعدة بدل حذفها.",
		)
	frappe.delete_doc("MS Alert Rule", rule, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": rule},
		"message_en": "Rule deleted.",
		"message_ar": "تم حذف القاعدة.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def rule_options(persona: str = None):
	"""Everything the rule builder needs, including a live preview count."""
	return {
		"triggers": [
			{
				"code": code,
				"label": meta["label"],
				"unit": meta["unit"],
				"group": meta["group"],
			}
			for code, meta in TRIGGERS.items()
		],
		"severities": [
			{"code": c, "label": l, "emoji": SEVERITY_EMOJI[c]} for c, l in SEVERITY_AR.items()
		],
		"levels": [{"code": c, "label": l} for c, l in LEVEL_AR.items()],
		"pages": [
			# Listed first: it is the widest possible action, and a school
			# reaching for it should not have to hunt past seven page names.
			{"path": BLOCK_EVERYTHING, "label": "الموقع بالكامل (عدا التنبيهات)"},
			*({"path": p, "label": l} for p, l in BLOCKABLE_PAGES.items()),
		],
		"operators": [
			{"code": ">=", "label": "أكبر من أو يساوي"},
			{"code": ">", "label": "أكبر من"},
			{"code": "<=", "label": "أقل من أو يساوي"},
			{"code": "<", "label": "أقل من"},
			{"code": "=", "label": "يساوي"},
		],
		"programs": frappe.get_all("Program", pluck="name", limit=100),
		"groups": [
			{"id": g.name, "name": g.student_group_name or g.name}
			for g in frappe.get_all(
				"Student Group",
				filters={"disabled": 0},
				fields=["name", "student_group_name"],
				limit=300,
			)
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def preview_rule(payload: str | dict, persona: str = None):
	"""How many students a rule *would* match, before saving it."""
	data = parse_json_arg(payload) or {}
	if data.get("trigger") not in TRIGGERS:
		return fail(message_en="Unknown trigger.", message_ar="شرط غير معروف.")

	draft = frappe._dict(
		{
			"name": "__preview__",
			"trigger": data["trigger"],
			"operator": data.get("operator") or ">=",
			"threshold": flt(data.get("threshold")),
			"within_days": cint(data.get("within_days")),
			"applies_to": data.get("applies_to") or "All",
			"program": data.get("program"),
			"student_group": data.get("student_group"),
		}
	)

	students = _scope_students(draft)
	measured = MEASURERS[draft.trigger](students, draft) if students else {}
	matched = {
		s: v for s, v in measured.items() if _compare(flt(v), draft.operator, flt(draft.threshold))
	}

	names = {
		r.name: r.student_name
		for r in frappe.get_all(
			"Student", filters={"name": ["in", list(matched)[:10] or [""]]},
			fields=["name", "student_name"],
		)
	}

	return {
		"scope": len(students),
		"measured": len(measured),
		"matched": len(matched),
		"sample": [
			{"student": s, "name": names.get(s, s), "value": flt(v)}
			for s, v in list(matched.items())[:10]
		],
		"unit": TRIGGERS[draft.trigger]["unit"],
	}


# --- Alerts API ------------------------------------------------------------


def _alert_payload(a) -> dict:
	return {
		"id": a.name,
		"student": a.student,
		"student_name": a.student_name,
		"rule": a.rule,
		"rule_name": a.rule_name,
		"trigger": a.trigger,
		"trigger_label": TRIGGERS.get(a.trigger, {}).get("label", a.trigger),
		"status": a.status,
		"status_label": STATUS_AR.get(a.status, a.status),
		"severity": a.severity,
		"severity_label": SEVERITY_AR.get(a.severity, a.severity),
		"severity_rank": SEVERITY_RANK.get(a.severity, 0),
		"level": a.level,
		"level_label": LEVEL_AR.get(a.level, a.level),
		"measured": flt(a.measured_value),
		"threshold": flt(a.threshold),
		"unit": TRIGGERS.get(a.trigger, {}).get("unit", ""),
		"title": a.title_ar,
		"message": a.message_ar,
		"emoji": a.emoji or SEVERITY_EMOJI.get(a.severity, "⚠️"),
		"raised_on": str(a.raised_on or ""),
		"escalated_on": str(a.escalated_on or ""),
		"resolved_on": str(a.resolved_on or ""),
		"acknowledged": bool(a.acknowledged_by_parent),
		"blocks_access": bool(a.blocks_access),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_alerts(
	student: str = None,
	status: str = None,
	severity: str = None,
	page: int = 1,
	page_size: int = 20,
	persona: str = None,
):
	"""Alerts the caller may see."""
	scope = resolve_scope(persona)
	filters = {}

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		if not allowed:
			return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)
		filters["student"] = student if student else ["in", allowed]
		# A dismissed alert is an administrative decision, not the family's business.
		filters["status"] = ["!=", "Dismissed"]
	elif persona == ROLE_TEACHER:
		from match_schools.api.students import _students_of_instructor

		mine = _students_of_instructor(scope.get("instructor"))
		if not mine:
			return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}
		filters["student"] = student if student in mine else ["in", mine]
	elif student:
		filters["student"] = student

	if status:
		filters["status"] = status
	if severity:
		filters["severity"] = severity

	total = frappe.db.count("MS Student Alert", filters)
	page, page_size, offset = paginate(page, page_size)
	rows = frappe.get_all(
		"MS Student Alert",
		filters=filters,
		fields=[
			"name", "student", "student_name", "rule", "rule_name", "trigger",
			"status", "severity", "level", "measured_value", "threshold",
			"title_ar", "message_ar", "emoji", "raised_on", "escalated_on",
			"resolved_on", "acknowledged_by_parent", "blocks_access",
		],
		order_by="raised_on desc",
		start=offset,
		page_length=page_size,
	)

	return {
		"items": [_alert_payload(r) for r in rows],
		"total": total,
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def student_file(student: str = None, persona: str = None):
	"""One student's whole alert history — the file the office keeps."""
	scope = resolve_scope(persona)
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = scope.get("students") or []
		student = student if student in allowed else (allowed[0] if allowed else None)
	if not student:
		return fail(message_en="No student given.", message_ar="لم يتم تحديد الطالب.")

	if persona == ROLE_TEACHER:
		from match_schools.api.students import _students_of_instructor

		if student not in _students_of_instructor(scope.get("instructor")):
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	rows = frappe.get_all(
		"MS Student Alert",
		filters={"student": student},
		fields=[
			"name", "student", "student_name", "rule", "rule_name", "trigger",
			"status", "severity", "level", "measured_value", "threshold",
			"title_ar", "message_ar", "emoji", "raised_on", "escalated_on",
			"resolved_on", "acknowledged_by_parent", "blocks_access",
		],
		order_by="raised_on desc",
		limit=200,
	)
	alerts = [_alert_payload(r) for r in rows]
	open_alerts = [a for a in alerts if a["status"] in ("Open", "Acknowledged", "Escalated")]

	name = frappe.db.get_value("Student", student, "student_name") or student

	return {
		"student": student,
		"student_name": name,
		"summary": {
			"total": len(alerts),
			"open": len(open_alerts),
			"warnings": sum(1 for a in alerts if a["level"] in ("Warning", "Final Warning")),
			"blocked": any(a["blocks_access"] for a in open_alerts),
			"worst": max((a["severity_rank"] for a in open_alerts), default=0),
			"unacknowledged": sum(1 for a in open_alerts if not a["acknowledged"]),
		},
		"alerts": alerts,
		"blocked_pages": sorted(
			{
				p
				for a in open_alerts
				if a["blocks_access"]
				for p in _pages_of(a["id"])
			}
		),
	}


def _pages_of(alert: str) -> list[str]:
	"""Which pages this alert blocks; stored on the alert when it was raised."""
	notes = frappe.db.get_value("MS Student Alert", alert, "resolution_notes") or ""
	return [
		p.strip()
		for p in notes.split(",")
		if p.strip() in BLOCKABLE_PAGES or p.strip() == BLOCK_EVERYTHING
	]


@frappe.whitelist()
@ms_endpoint(ROLE_STUDENT, ROLE_PARENT, ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def my_blocks(persona: str = None):
	"""Which pages the caller is currently blocked from, and why.

	Staff are never blocked — a rule targets students.
	"""
	if persona in BACK_OFFICE or persona == ROLE_TEACHER:
		return {"blocked": [], "reasons": []}

	scope = resolve_scope(persona)
	students = scope.get("students") or []
	if not students:
		return {"blocked": [], "reasons": []}

	rows = frappe.get_all(
		"MS Student Alert",
		filters={
			"student": ["in", students],
			"blocks_access": 1,
			"status": ["in", ["Open", "Acknowledged", "Escalated"]],
		},
		fields=[
			"name", "student_name", "title_ar", "message_ar", "emoji", "severity",
			"trigger", "rule",
		],
	)

	# A block raised over unpaid fees locks the guardian out, not the child:
	# a student shut out of their timetable and homework over a bill they have
	# no part in is a punishment aimed at the wrong person.
	rows = _visible_to(persona, rows)

	blocked: set[str] = set()
	reasons = []
	for r in rows:
		pages = _pages_of(r.name)
		blocked |= set(pages)
		reasons.append(
			{
				"alert": r.name,
				"student_name": r.student_name,
				"title": r.title_ar,
				"message": r.message_ar,
				"emoji": r.emoji,
				"severity": r.severity,
				"pages": [
					"الموقع بالكامل" if p == BLOCK_EVERYTHING else BLOCKABLE_PAGES.get(p, p)
					for p in pages
				],
			}
		)

	# `everything` is reported separately rather than as a page in the list:
	# the guard has to close routes it has never heard of, which a list of
	# known paths cannot do.
	return {
		"blocked": sorted(p for p in blocked if p != BLOCK_EVERYTHING),
		"everything": BLOCK_EVERYTHING in blocked,
		"allowed": list(ALWAYS_ALLOWED),
		"reasons": reasons,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_PARENT, ROLE_STUDENT)
def my_alerts(persona: str = None):
	"""Alerts the caller has not yet acknowledged.

	`my_blocks` answers "which pages am I locked out of", which is only the
	last step of an escalation. A first warning blocks nothing, so a student
	saw no sign of it at all until the day access was cut — the point of a
	warning is that it arrives before that.

	Returned newest and most severe first, so a dialog can show the one that
	matters without the caller having to rank them.
	"""
	if persona in BACK_OFFICE or persona == ROLE_TEACHER:
		return {"alerts": []}

	scope = resolve_scope(persona)
	students = scope.get("students") or []
	if not students:
		return {"alerts": []}

	rows = frappe.get_all(
		"MS Student Alert",
		filters={
			"student": ["in", students],
			"status": ["in", ["Open", "Escalated"]],
			# Acknowledged means "I have seen this"; it stays on the student's
			# file but stops interrupting them on every page load.
			"acknowledged_by_parent": 0,
		},
		fields=[
			"name", "student", "student_name", "title_ar", "message_ar", "emoji",
			"severity", "level", "measured_value", "threshold", "raised_on",
			"blocks_access", "trigger", "rule",
		],
		limit=20,
	)

	rows = _visible_to(persona, rows)

	for r in rows:
		r["severity_label"] = SEVERITY_AR.get(r.severity, r.severity)
		r["level_label"] = LEVEL_AR.get(r.level, r.level)
		r["pages"] = [BLOCKABLE_PAGES.get(p, p) for p in _pages_of(r.name)]

	# Most serious first, then most recent: a final warning outranks an
	# information notice raised an hour later.
	rows.sort(
		key=lambda r: (SEVERITY_RANK.get(r["severity"], 0), str(r["raised_on"] or "")),
		reverse=True,
	)
	return {"alerts": rows, "count": len(rows)}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_STUDENT, ROLE_PARENT)
def acknowledge_alert(alert: str, persona: str = None):
	"""A parent confirms they have seen the warning."""
	doc = frappe.get_doc("MS Student Alert", alert)
	scope = resolve_scope(persona)
	if doc.student not in (scope.get("students") or []):
		frappe.throw(_("This alert is not yours."), frappe.PermissionError)

	doc.acknowledged_by_parent = 1
	doc.acknowledged_on = now_datetime()
	if doc.status == "Open":
		doc.status = "Acknowledged"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": alert, "status": doc.status},
		"message_en": "Acknowledged.",
		"message_ar": "تم تسجيل اطّلاعك على التنبيه.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def resolve_alert(alert: str, notes: str = None, dismiss: int = 0, persona: str = None):
	"""Close an alert — resolved, or dismissed as not applicable."""
	doc = frappe.get_doc("MS Student Alert", alert)
	doc.status = "Dismissed" if cint(dismiss) else "Resolved"
	doc.resolved_on = now_datetime()
	doc.resolved_by = frappe.session.user
	if notes:
		doc.resolution_notes = notes
	# Closing an alert must lift whatever it was blocking.
	doc.blocks_access = 0
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": alert, "status": doc.status},
		"message_en": "Closed.",
		"message_ar": "تم إلغاء التنبيه." if cint(dismiss) else "تمت معالجة التنبيه.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def alerts_overview(persona: str = None):
	"""The administration's dashboard over every open alert."""
	rows = frappe.get_all(
		"MS Student Alert",
		filters={"status": ["in", ["Open", "Acknowledged", "Escalated"]]},
		fields=[
			"name", "student", "student_name", "rule_name", "trigger", "status",
			"severity", "level", "measured_value", "threshold", "title_ar",
			"message_ar", "emoji", "raised_on", "escalated_on", "resolved_on",
			"acknowledged_by_parent", "blocks_access", "rule",
		],
		order_by="raised_on desc",
		limit=500,
	)
	alerts = [_alert_payload(r) for r in rows]

	by_severity: dict[str, int] = {}
	by_trigger: dict[str, int] = {}
	for a in alerts:
		by_severity[a["severity"]] = by_severity.get(a["severity"], 0) + 1
		by_trigger[a["trigger_label"]] = by_trigger.get(a["trigger_label"], 0) + 1

	return {
		"summary": {
			"open": len(alerts),
			"blocked": sum(1 for a in alerts if a["blocks_access"]),
			"unacknowledged": sum(1 for a in alerts if not a["acknowledged"]),
			"critical": by_severity.get("Critical", 0),
			"students": len({a["student"] for a in alerts}),
		},
		"by_severity": [
			{"severity": s, "label": SEVERITY_AR.get(s, s), "count": c, "emoji": SEVERITY_EMOJI.get(s)}
			for s, c in sorted(by_severity.items(), key=lambda x: -SEVERITY_RANK.get(x[0], 0))
		],
		"by_trigger": [{"trigger": t, "count": c} for t, c in sorted(by_trigger.items(), key=lambda x: -x[1])],
		"alerts": alerts,
	}
