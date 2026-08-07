# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Seed fee invoices and assessment results for the demo school.

These exercise the fees / exams / grades endpoints, which otherwise return
empty lists and prove nothing.

Run with:
    bench --site <site> execute match_schools.setup.demo_finance.create_demo_finance
"""

import random

import frappe
from frappe.utils import add_days, add_months, flt, getdate, today

FEE_CATEGORIES = [
	("رسوم دراسية", 2200),
	("رسوم كتب", 350),
	("رسوم أنشطة", 250),
	("رسوم مواصلات", 700),
]

ASSESSMENT_GROUPS = ["الفصل الأول", "الفصل الثاني", "امتحان نصفي", "امتحان نهائي"]

GRADE_INTERVALS = [
	("A+", 95, "ممتاز مرتفع"),
	("A", 90, "ممتاز"),
	("B+", 85, "جيد جداً مرتفع"),
	("B", 80, "جيد جداً"),
	("C+", 75, "جيد مرتفع"),
	("C", 65, "جيد"),
	("D", 50, "مقبول"),
	("F", 0, "راسب"),
]


def create_demo_finance(students_per_group: int = 6, groups_limit: int = 6):
	"""Create fee structures + invoices and assessment plans + results."""
	random.seed(7)
	frappe.flags.in_import = True

	company = _pick_company()
	income_account = _ensure_income_account(company)
	receivable = frappe.db.get_value("Company", company, "default_receivable_account")

	categories = _ensure_fee_categories()
	grading_scale = _ensure_grading_scale()
	_ensure_assessment_groups()

	groups = frappe.get_all(
		"Student Group",
		filters={"disabled": 0},
		fields=["name", "program", "academic_year", "academic_term"],
		order_by="name",
		limit=groups_limit,
	)

	fees_created = _create_fees(groups, company, income_account, receivable, categories, students_per_group)
	results_created = _create_assessments(groups, grading_scale, students_per_group)
	linked = _link_demo_personas()

	frappe.db.commit()
	return {
		"company": company,
		"fee_invoices": fees_created,
		"assessment_results": results_created,
		"demo_links": linked,
	}


def _link_demo_personas() -> dict:
	"""Point the demo student/parent logins at students that actually have
	fees and grades, so those dashboards are not empty."""
	rows = frappe.db.sql(
		"""
		SELECT f.student
		FROM `tabFees` f
		WHERE f.docstatus = 1
		  AND EXISTS (
			SELECT 1 FROM `tabAssessment Result` ar
			WHERE ar.student = f.student AND ar.docstatus = 1
		  )
		GROUP BY f.student
		ORDER BY f.student
		LIMIT 2
		""",
		as_dict=True,
	)
	if not rows:
		return {}

	student_user = "student@match-edu.ps"
	result = {}

	if frappe.db.exists("User", student_user):
		target = rows[0].student
		# Clear any previous link so only one student maps to this login.
		frappe.db.sql("UPDATE `tabStudent` SET user = NULL WHERE user = %s", (student_user,))
		frappe.db.set_value("Student", target, "user", student_user)
		result["student"] = target

	guardian = frappe.db.get_value("Guardian", {"user": "parent@match-edu.ps"}, "name")
	if guardian:
		children = []
		for r in rows:
			doc = frappe.get_doc("Student", r.student)
			if not any(g.guardian == guardian for g in doc.guardians):
				doc.append("guardians", {"guardian": guardian, "relation": "Father"})
				doc.save(ignore_permissions=True)
			children.append(r.student)
		result["guardian"] = guardian
		result["children"] = children

	result["instructor_groups"] = _assign_demo_teacher()
	return result


def _assign_demo_teacher() -> list[str]:
	"""Attach the demo teacher to groups that actually have seeded results,
	otherwise their dashboard and reports come back empty."""
	full_name = frappe.db.get_value("User", "teacher@match-edu.ps", "full_name")
	if not full_name:
		return []
	instructor = frappe.db.get_value("Instructor", {"instructor_name": full_name}, "name")
	if not instructor:
		return []

	groups = [
		r.student_group
		for r in frappe.db.sql(
			"SELECT DISTINCT student_group FROM `tabAssessment Result` WHERE docstatus = 1",
			as_dict=True,
		)
		if r.student_group
	][:3]

	added = []
	for group in groups:
		doc = frappe.get_doc("Student Group", group)
		if not any(i.instructor == instructor for i in doc.instructors):
			doc.append("instructors", {"instructor": instructor})
			doc.save(ignore_permissions=True)
		added.append(group)
	return added


def _pick_company() -> str:
	"""Prefer the company the education data already points at."""
	company = frappe.defaults.get_defaults().get("company")
	if company and frappe.db.exists("Company", company):
		return company
	return frappe.db.get_value("Company", {}, "name")


def _ensure_income_account(company: str) -> str:
	abbr = frappe.db.get_value("Company", company, "abbr")
	name = f"4110 - Sales - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	# Fall back to any non-group income account for this company.
	acct = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
	)
	return acct


def _ensure_fee_categories() -> list[tuple[str, float]]:
	out = []
	for label, amount in FEE_CATEGORIES:
		if not frappe.db.exists("Fee Category", label):
			frappe.get_doc(
				{"doctype": "Fee Category", "category_name": label, "description": label}
			).insert(ignore_permissions=True)
		out.append((label, amount))
	return out


def _ensure_grading_scale() -> str:
	name = "المقياس المدرسي"
	if frappe.db.exists("Grading Scale", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Grading Scale",
			"grading_scale_name": name,
			"description": "مقياس التقييم المدرسي",
		}
	)
	for code, threshold, label in GRADE_INTERVALS:
		doc.append(
			"intervals",
			{"grade_code": code, "threshold": threshold, "grade_description": label},
		)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _ensure_assessment_groups():
	# The tree root is the group with no parent; NULL and '' both occur.
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabAssessment Group`
		WHERE is_group = 1 AND (parent_assessment_group IS NULL OR parent_assessment_group = '')
		LIMIT 1
		""",
		as_dict=True,
	)
	root = rows[0].name if rows else None
	if not root:
		root = (
			frappe.get_doc(
				{
					"doctype": "Assessment Group",
					"assessment_group_name": "All Assessment Groups",
					"is_group": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	for label in ASSESSMENT_GROUPS:
		if frappe.db.exists("Assessment Group", label):
			continue
		frappe.get_doc(
			{
				"doctype": "Assessment Group",
				"assessment_group_name": label,
				"parent_assessment_group": root,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)


def _create_fees(groups, company, income_account, receivable, categories, per_group) -> int:
	created = 0

	for group in groups:
		structure = _ensure_fee_structure(group, company, receivable, categories)

		students = frappe.get_all(
			"Student Group Student",
			filters={"parent": group.name, "parenttype": "Student Group", "active": 1},
			fields=["student", "student_name"],
			limit=per_group,
		)

		for idx, s in enumerate(students):
			if frappe.db.exists(
				"Fees",
				{
					"student": s.student,
					"program": group.program,
					"academic_year": group.academic_year,
					"docstatus": ["<", 2],
				},
			):
				continue

			# Fees validates that the student is enrolled in the program.
			enrollment = frappe.db.get_value(
				"Program Enrollment",
				{
					"student": s.student,
					"program": group.program,
					"academic_year": group.academic_year,
					"docstatus": 1,
				},
				"name",
			)
			if not enrollment:
				continue

			posting = getdate(add_months(today(), -(idx % 4)))
			doc = frappe.get_doc(
				{
					"doctype": "Fees",
					"student": s.student,
					"student_name": s.student_name,
					"fee_structure": structure,
					"program_enrollment": enrollment,
					"program": group.program,
					"academic_year": group.academic_year,
					"academic_term": group.academic_term,
					"company": company,
					"posting_date": posting,
					"due_date": add_days(posting, 30),
					"receivable_account": receivable,
					"income_account": income_account,
				}
			)
			for label, amount in categories:
				doc.append("components", {"fees_category": label, "amount": amount})
			doc.insert(ignore_permissions=True)
			doc.submit()

			# Spread payment states so the UI shows paid / partial / late.
			_apply_payment_state(doc, idx)
			created += 1

	return created


def _apply_payment_state(fees_doc, idx: int):
	"""Set outstanding directly to model paid / partial / unpaid invoices.

	Recording real Payment Entries would need a full banking setup; for demo
	purposes the outstanding amount is what every fee endpoint reads.
	"""
	total = flt(fees_doc.grand_total)
	bucket = idx % 3
	if bucket == 0:
		outstanding = 0.0            # fully paid
	elif bucket == 1:
		outstanding = round(total * 0.4, 2)   # partially paid
	else:
		outstanding = total          # nothing paid yet

	if outstanding != flt(fees_doc.outstanding_amount):
		frappe.db.set_value("Fees", fees_doc.name, "outstanding_amount", outstanding)


def _ensure_fee_structure(group, company, receivable, categories) -> str:
	label = f"هيكل رسوم {group.program}"
	existing = frappe.db.get_value(
		"Fee Structure",
		{"program": group.program, "academic_year": group.academic_year, "docstatus": 1},
		"name",
	)
	if existing:
		return existing

	# Fee Structure has no income_account field (only Fees does).
	doc = frappe.get_doc(
		{
			"doctype": "Fee Structure",
			"program": group.program,
			"academic_year": group.academic_year,
			"academic_term": group.academic_term,
			"company": company,
			"receivable_account": receivable,
		}
	)
	for cat_label, amount in categories:
		doc.append("components", {"fees_category": cat_label, "amount": amount})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _create_assessments(groups, grading_scale, per_group) -> int:
	created = 0
	assessment_group = ASSESSMENT_GROUPS[1]  # "الفصل الثاني"

	# Each plan gets a unique (day, room) pair; Education rejects overlapping
	# plans for the same student group *and* for the same room.
	slot_counter = 0

	for group in groups:
		courses = _courses_of_program(group.program)[:3]
		students = frappe.get_all(
			"Student Group Student",
			filters={"parent": group.name, "parenttype": "Student Group", "active": 1},
			fields=["student", "student_name"],
			limit=per_group,
		)
		if not students or not courses:
			continue

		for course in courses:
			plan = _ensure_assessment_plan(
				group, course, assessment_group, grading_scale, slot_counter
			)
			slot_counter += 1
			if not plan:
				continue

			for s in students:
				if frappe.db.exists(
					"Assessment Result",
					{"assessment_plan": plan, "student": s.student, "docstatus": ["<", 2]},
				):
					continue
				score = random.randint(45, 99)
				doc = frappe.get_doc(
					{
						"doctype": "Assessment Result",
						"assessment_plan": plan,
						"student": s.student,
						"student_name": s.student_name,
						"student_group": group.name,
						"course": course,
						"program": group.program,
						"academic_year": group.academic_year,
						"academic_term": group.academic_term,
						"assessment_group": assessment_group,
						"grading_scale": grading_scale,
						"maximum_score": 100,
					}
				)
				# Assessment Result computes totals from its detail rows.
				doc.append(
					"details",
					{
						"assessment_criteria": _default_criteria(),
						"maximum_score": 100,
						"score": score,
					},
				)
				doc.insert(ignore_permissions=True)
				doc.submit()
				created += 1

	return created


def _courses_of_program(program: str) -> list[str]:
	return [
		r.course
		for r in frappe.get_all(
			"Program Course",
			filters={"parent": program, "parenttype": "Program"},
			fields=["course"],
			order_by="idx",
		)
	]


def _default_criteria() -> str:
	name = "التقييم العام"
	if not frappe.db.exists("Assessment Criteria", name):
		frappe.get_doc(
			{
				"doctype": "Assessment Criteria",
				"assessment_criteria": name,
			}
		).insert(ignore_permissions=True)
	return name


def _ensure_assessment_plan(group, course, assessment_group, grading_scale, slot: int = 0) -> str | None:
	title = f"{course} - {group.name}"
	existing = frappe.db.get_value(
		"Assessment Plan",
		{"student_group": group.name, "course": course, "assessment_group": assessment_group},
		"name",
	)
	if existing:
		return existing

	instructor = frappe.db.get_value(
		"Student Group Instructor",
		{"parent": group.name, "parenttype": "Student Group"},
		"instructor",
	)
	rooms = frappe.get_all("Room", pluck="name", order_by="name")
	room = rooms[slot % len(rooms)] if rooms else None

	doc = frappe.get_doc(
		{
			"doctype": "Assessment Plan",
			"assessment_name": title,
			"student_group": group.name,
			"course": course,
			"program": group.program,
			"academic_year": group.academic_year,
			"academic_term": group.academic_term,
			"assessment_group": assessment_group,
			"grading_scale": grading_scale,
			"maximum_assessment_score": 100,
			# Every plan gets its own day. Education rejects a plan that
			# overlaps another plan *or* a Course Schedule, for either the
			# same student group or the same room, so one-per-day is the
			# simplest way to stay conflict-free. Times sit after the last
			# demo class (which ends 11:45) to avoid the schedule clash.
			"schedule_date": add_days(today(), 7 + slot),
			"from_time": "13:00:00",
			"to_time": "14:30:00",
			"room": room,
			"examiner": instructor,
			"supervisor": instructor,
		}
	)
	doc.append(
		"assessment_criteria",
		{"assessment_criteria": _default_criteria(), "maximum_score": 100},
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name
