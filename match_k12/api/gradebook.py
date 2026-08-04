# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The gradebook: mark entry, term grades and the student's academic record.

A subject's final mark is built from weighted components — exams, quizzes,
activities, homework — defined by a K12 Grade Scheme. Bonus components sit
outside the 100% and are added on top, capped so a student cannot exceed full
marks.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, today

from match_k12.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	k12_endpoint,
	parse_json_arg,
	resolve_scope,
)

COMPONENT_TYPE_AR = {
	"Exam": "امتحان",
	"Quiz": "اختبار قصير",
	"Activity": "نشاط",
	"Homework": "واجب",
	"Participation": "مشاركة",
	"Project": "مشروع",
	"Bonus": "درجة إضافية",
}

# Letter grade bands, with the emoji the UI shows next to each mark.
GRADE_BANDS = [
	(95, "A+", "ممتاز مرتفع", "🌟"),
	(90, "A", "ممتاز", "🎉"),
	(85, "B+", "جيد جداً مرتفع", "😃"),
	(80, "B", "جيد جداً", "🙂"),
	(75, "C+", "جيد مرتفع", "😊"),
	(65, "C", "جيد", "👍"),
	(50, "D", "مقبول", "😐"),
	(0, "F", "راسب", "❌"),
]


def grade_for(percentage: float) -> dict:
	"""Letter, Arabic label and emoji for a percentage."""
	pct = flt(percentage)
	for threshold, letter, label, emoji in GRADE_BANDS:
		if pct >= threshold:
			return {"grade": letter, "label": label, "emoji": emoji, "percentage": round(pct, 1)}
	last = GRADE_BANDS[-1]
	return {"grade": last[1], "label": last[2], "emoji": last[3], "percentage": round(pct, 1)}


def _assert_can_see(persona: str, student: str):
	if persona in BACK_OFFICE:
		return
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		from match_k12.api.students import _students_of_instructor

		if student not in _students_of_instructor(scope.get("instructor")):
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)
		return
	if student not in (scope.get("students") or []):
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)


# --- Grade schemes ---------------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def list_schemes(course: str = None, program: str = None, persona: str = None):
	filters = {}
	if course:
		filters["course"] = course
	if program:
		filters["program"] = program

	rows = frappe.get_all(
		"K12 Grade Scheme",
		filters=filters,
		fields=[
			"name", "scheme_name", "course", "program", "academic_year",
			"academic_term", "is_default", "total_weight",
		],
		order_by="scheme_name",
	)
	for r in rows:
		r["components"] = frappe.get_all(
			"K12 Grade Scheme Component",
			filters={"parent": r["name"], "parenttype": "K12 Grade Scheme"},
			fields=["component_name", "component_type", "weight", "max_score"],
			order_by="idx",
		)
		for c in r["components"]:
			c["type_label"] = COMPONENT_TYPE_AR.get(c["component_type"], c["component_type"])
		r["id"] = r["name"]
	return rows


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_scheme(payload: str | dict, persona: str = None):
	"""Create or update a weighting scheme.

	A teacher may adjust the plan for a subject they teach — the weighting is
	a teaching decision, and marks are never validated against it anyway.
	They cannot touch a school-wide default.
	"""
	from match_k12.api.gradeflow import assert_teacher_owns_course

	data = parse_json_arg(payload) or {}
	if not data.get("scheme_name"):
		return fail(message_en="Scheme name is required.", message_ar="اسم الخطة مطلوب.")

	if persona == ROLE_TEACHER:
		if not data.get("course"):
			return fail(
				message_en="Choose the subject this plan applies to.",
				message_ar="اختر المادة التي تخصها هذه الخطة.",
			)
		assert_teacher_owns_course(persona, data["course"])
		# A school-wide default is the administration's to set.
		data["is_default"] = 0

	components = data.get("components") or []
	if not components:
		return fail(
			message_en="Add at least one component.",
			message_ar="أضف مكوّناً واحداً على الأقل.",
		)

	fields = {
		k: data.get(k)
		for k in ("scheme_name", "course", "program", "academic_year", "academic_term", "is_default")
		if data.get(k) is not None
	}

	scheme_id = data.get("id") or data.get("name")
	doc = (
		frappe.get_doc("K12 Grade Scheme", scheme_id)
		if scheme_id
		else frappe.new_doc("K12 Grade Scheme")
	)
	doc.update(fields)
	doc.set("components", [])
	for c in components:
		doc.append(
			"components",
			{
				"component_name": c.get("component_name"),
				"component_type": c.get("component_type") or "Exam",
				"weight": flt(c.get("weight")),
				"max_score": flt(c.get("max_score")) or 100,
			},
		)

	doc.save() if scheme_id else doc.insert()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "total_weight": flt(doc.total_weight)},
		"message_en": "Scheme saved.",
		"message_ar": "تم حفظ خطة التقييم.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_scheme(scheme: str, persona: str = None):
	from match_k12.api.gradeflow import assert_teacher_owns_course

	doc = frappe.db.get_value(
		"K12 Grade Scheme", scheme, ["course", "is_default"], as_dict=True
	)
	if not doc:
		return fail(message_en="Scheme not found.", message_ar="لم يتم العثور على الخطة.")
	if persona == ROLE_TEACHER:
		if cint(doc.is_default):
			return fail(
				message_en="A school-wide default can only be removed by the administration.",
				message_ar="الخطة الافتراضية للمدرسة تُحذف من الإدارة فقط.",
			)
		assert_teacher_owns_course(persona, doc.course)

	frappe.delete_doc("K12 Grade Scheme", scheme)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": scheme},
		"message_en": "Scheme deleted.",
		"message_ar": "تم حذف الخطة.",
	}


def resolve_scheme(course: str, program: str = None) -> dict | None:
	"""Best matching scheme: course+program, then course, then a global default."""
	for filters in (
		{"course": course, "program": program},
		{"course": course},
		{"is_default": 1, "course": ["in", ["", None]]},
	):
		clean = {k: v for k, v in filters.items() if v not in (None, "")}
		if not clean:
			continue
		name = frappe.db.get_value("K12 Grade Scheme", clean, "name")
		if name:
			doc = frappe.get_doc("K12 Grade Scheme", name)
			return {
				"id": doc.name,
				"scheme_name": doc.scheme_name,
				"components": [
					{
						"component_name": c.component_name,
						"component_type": c.component_type,
						"weight": flt(c.weight),
						"max_score": flt(c.max_score),
					}
					for c in doc.components
				],
			}
	return None


# --- Mark entry ------------------------------------------------------------


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_entry_sheet(
	student_group: str,
	course: str,
	component_name: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""The class list for one subject, with any marks already entered.

	Returns the scheme's components so the UI can offer them as tabs.
	"""
	from match_k12.api.gradeflow import (
		STATUS_AR,
		assert_teacher_owns_course,
		submission_status,
	)

	assert_teacher_owns_course(persona, course)

	group = frappe.db.get_value(
		"Student Group", student_group, ["name", "program", "academic_year", "academic_term"], as_dict=True
	)
	if not group:
		return fail(message_en="Class not found.", message_ar="لم يتم العثور على الشعبة.")

	academic_term = academic_term or group.academic_term or get_default_academic_term()
	academic_year = group.academic_year or get_default_academic_year()
	scheme = resolve_scheme(course, group.program)

	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name", "group_roll_number"],
		order_by="group_roll_number, student_name",
	)

	entry_filters = {
		"student_group": student_group,
		"course": course,
		"academic_year": academic_year,
	}
	if academic_term:
		entry_filters["academic_term"] = academic_term
	if component_name:
		entry_filters["component_name"] = component_name

	existing = frappe.get_all(
		"K12 Gradebook Entry",
		filters=entry_filters,
		fields=[
			"name", "student", "component_name", "component_type",
			"score", "max_score", "weight", "is_bonus", "remarks",
		],
	)
	by_student: dict[str, list] = {}
	for e in existing:
		by_student.setdefault(e.student, []).append(e)

	rows = []
	for s in roster:
		marks = by_student.get(s.student, [])
		current = next((m for m in marks if m.component_name == component_name), None) if component_name else None
		rows.append(
			{
				"student": s.student,
				"student_name": s.student_name,
				"roll_number": s.group_roll_number,
				"entry_id": current.name if current else None,
				"score": flt(current.score) if current else None,
				"max_score": flt(current.max_score) if current else None,
				"remarks": current.remarks if current else None,
				"entries": [
					{
						"id": m.name,
						"component_name": m.component_name,
						"component_type": m.component_type,
						"type_label": COMPONENT_TYPE_AR.get(m.component_type, m.component_type),
						"score": flt(m.score),
						"max_score": flt(m.max_score),
						"is_bonus": bool(m.is_bonus),
					}
					for m in marks
				],
			}
		)

	return {
		"student_group": student_group,
		"course": course,
		"program": group.program,
		"academic_year": academic_year,
		"academic_term": academic_term,
		"component_name": component_name,
		"scheme": scheme,
		"components": (scheme or {}).get("components", []),
		"rows": rows,
		"entered": sum(1 for r in rows if r["entry_id"]),
		"total": len(rows),
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_marks(payload: str | dict, persona: str = None):
	"""Save a whole column of marks at once.

	payload = {
	  student_group, course, academic_year, academic_term,
	  component_name, component_type, max_score, weight, is_bonus,
	  marks: [{student, score, remarks}]
	}
	"""
	data = parse_json_arg(payload) or {}
	required = ("student_group", "course", "component_name")
	missing = [r for r in required if not data.get(r)]
	if missing:
		return fail(
			message_en=f"Missing: {', '.join(missing)}.",
			message_ar="بيانات ناقصة: الشعبة، المادة، واسم المكوّن مطلوبة.",
		)

	marks = data.get("marks") or []
	if not marks:
		return fail(message_en="No marks supplied.", message_ar="لم يتم إرسال أي درجات.")

	from match_k12.api.gradeflow import assert_entry_allowed, assert_teacher_owns_course

	assert_teacher_owns_course(persona, data["course"])

	group = frappe.db.get_value(
		"Student Group",
		data["student_group"],
		["program", "academic_year", "academic_term"],
		as_dict=True,
	)
	academic_year = data.get("academic_year") or (group.academic_year if group else None) or get_default_academic_year()
	academic_term = data.get("academic_term") or (group.academic_term if group else None)

	# Once the marks are with the administration the teacher may not edit them.
	assert_entry_allowed(persona, data["student_group"], data["course"], academic_term)

	component_type = data.get("component_type") or "Exam"
	is_bonus = cint(data.get("is_bonus")) or (1 if component_type == "Bonus" else 0)
	max_score = flt(data.get("max_score")) or 100
	weight = flt(data.get("weight"))

	saved, updated, skipped = 0, 0, []

	for row in marks:
		student = row.get("student")
		if not student:
			continue
		# An empty score means "not entered" — remove any previous value.
		raw = row.get("score")
		if raw in (None, ""):
			existing = frappe.db.get_value(
				"K12 Gradebook Entry",
				{
					"student": student,
					"course": data["course"],
					"component_name": data["component_name"],
					"academic_year": academic_year,
				},
				"name",
			)
			if existing:
				frappe.delete_doc("K12 Gradebook Entry", existing, ignore_permissions=True)
			continue

		score = flt(raw)
		if not is_bonus and score > max_score:
			skipped.append(row.get("student_name") or student)
			continue

		existing = frappe.db.get_value(
			"K12 Gradebook Entry",
			{
				"student": student,
				"course": data["course"],
				"component_name": data["component_name"],
				"academic_year": academic_year,
			},
			"name",
		)

		values = {
			"student": student,
			"course": data["course"],
			"student_group": data["student_group"],
			"program": group.program if group else None,
			"academic_year": academic_year,
			"academic_term": academic_term,
			"assessment_plan": data.get("assessment_plan"),
			"component_name": data["component_name"],
			"component_type": component_type,
			"score": score,
			"max_score": max_score,
			"weight": weight,
			"is_bonus": is_bonus,
			"remarks": row.get("remarks"),
		}

		if existing:
			doc = frappe.get_doc("K12 Gradebook Entry", existing)
			doc.update(values)
			doc.save()
			updated += 1
		else:
			doc = frappe.get_doc({"doctype": "K12 Gradebook Entry", **values})
			doc.insert()
			saved += 1

	frappe.db.commit()

	message_ar = f"تم حفظ {saved + updated} درجة."
	if skipped:
		message_ar += f" تم تجاوز {len(skipped)} درجة تفوق الحد الأقصى."

	return {
		"success": True,
		"data": {"created": saved, "updated": updated, "skipped": skipped},
		"message_en": f"Saved {saved + updated} marks.",
		"message_ar": message_ar,
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_mark(entry: str, persona: str = None):
	frappe.delete_doc("K12 Gradebook Entry", entry)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": entry},
		"message_en": "Mark deleted.",
		"message_ar": "تم حذف الدرجة.",
	}


# --- Term grades and the academic record -----------------------------------


def _compute_subject_grade(entries: list[dict]) -> dict:
	"""Weighted percentage for one subject, with bonus added on top.

	Each component contributes (score / max) * weight. When the entered
	components do not cover the full 100% — say only two of four are in — the
	result is scaled to what has been entered, so a partial term still reads
	sensibly, and `covered` reports how much of the scheme is done.
	"""
	# A row counts as bonus if flagged OR typed as Bonus — save_marks sets the
	# flag, but an entry created directly may only carry the type.
	def _is_bonus(e):
		return bool(e.get("is_bonus")) or e.get("component_type") == "Bonus"

	graded = [e for e in entries if not _is_bonus(e)]
	bonus = [e for e in entries if _is_bonus(e)]

	weighted_sum = 0.0
	covered = 0.0
	for e in graded:
		max_score = flt(e.get("max_score"))
		if not max_score:
			continue
		weight = flt(e.get("weight"))
		ratio = flt(e.get("score")) / max_score
		if weight:
			weighted_sum += ratio * weight
			covered += weight
		else:
			# No weight given: treat every such component equally.
			weighted_sum += ratio * 100
			covered += 100

	percentage = (weighted_sum / covered * 100) if covered else 0.0

	bonus_points = sum(
		flt(e.get("score")) / flt(e.get("max_score")) * flt(e.get("weight") or 0)
		for e in bonus
		if flt(e.get("max_score"))
	)
	# Bonus lifts the mark but can never push it past 100.
	final = min(percentage + bonus_points, 100.0)

	# grade_for() also returns a "percentage"; take only the label fields so it
	# cannot overwrite the pre-bonus percentage computed above.
	band = grade_for(final)
	return {
		"percentage": round(percentage, 1),
		"bonus": round(bonus_points, 1),
		"final": round(final, 1),
		"covered": round(covered, 1),
		"grade": band["grade"],
		"label": band["label"],
		"emoji": band["emoji"],
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def term_grades(
	student: str,
	academic_year: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Every subject's marks and computed grade for one term."""
	_assert_can_see(persona, student)

	academic_year = academic_year or get_default_academic_year()

	filters = {"student": student}
	if academic_year:
		filters["academic_year"] = academic_year
	if academic_term:
		filters["academic_term"] = academic_term

	entries = frappe.get_all(
		"K12 Gradebook Entry",
		filters=filters,
		fields=[
			"name", "course", "component_name", "component_type", "score",
			"max_score", "weight", "is_bonus", "percentage", "remarks",
			"academic_term", "entry_date",
		],
		order_by="course, entry_date",
	)

	from match_k12.api.gradeflow import courses_of_instructor, term_is_published

	# A teacher sees only the subjects they teach — never a student's marks in
	# someone else's subject.
	own_courses = None
	if persona == ROLE_TEACHER:
		own_courses = courses_of_instructor(resolve_scope(persona).get("instructor"))
		entries = [e for e in entries if e.course in own_courses]

	by_course: dict[str, list] = {}
	for e in entries:
		by_course.setdefault(e.course, []).append(e)

	subjects = []
	for course, rows in by_course.items():
		computed = _compute_subject_grade(rows)
		subjects.append(
			{
				"course": course,
				"components": [
					{
						"id": r.name,
						"component_name": r.component_name,
						"component_type": r.component_type,
						"type_label": COMPONENT_TYPE_AR.get(r.component_type, r.component_type),
						"score": flt(r.score),
						"max_score": flt(r.max_score),
						"weight": flt(r.weight),
						"percentage": flt(r.percentage),
						"is_bonus": bool(r.is_bonus),
						"remarks": r.remarks,
						**grade_for(flt(r.percentage)),
					}
					for r in rows
				],
				**computed,
			}
		)

	subjects.sort(key=lambda s: s["course"] or "")

	overall = (
		round(sum(s["final"] for s in subjects) / len(subjects), 1) if subjects else 0.0
	)

	student_doc = frappe.db.get_value(
		"Student", student, ["student_name", "image"], as_dict=True
	) or {}

	# The term total belongs to the administration. A teacher only ever sees
	# their own subject, so an average across subjects is meaningless to them
	# and would leak other teachers' marks. Students and parents see it only
	# once the administration has published the term.
	# Publication is recorded per term, so fall back to the active term rather
	# than treating "unspecified" as "not published".
	published = term_is_published(student, academic_term or get_default_academic_term())
	show_overall = persona in BACK_OFFICE or (
		persona in (ROLE_STUDENT, ROLE_PARENT) and published
	)

	result = {
		"student": student,
		"student_name": student_doc.get("student_name"),
		"image": student_doc.get("image"),
		"academic_year": academic_year,
		"academic_term": academic_term,
		"subjects": subjects,
		"subject_count": len(subjects),
		"published": published,
		"shows_overall": show_overall,
	}
	if show_overall:
		result["overall"] = overall
		result["overall_grade"] = grade_for(overall)
	else:
		result["overall"] = None
		result["overall_grade"] = None
		result["overall_hidden_reason"] = (
			"teacher_scope"
			if persona == ROLE_TEACHER
			else "not_published"
		)
	return result


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def academic_record(student: str, persona: str = None):
	"""The student's whole history: every year and term, with final grades."""
	_assert_can_see(persona, student)

	periods = frappe.db.sql(
		"""
		SELECT DISTINCT academic_year, academic_term
		FROM `tabK12 Gradebook Entry`
		WHERE student = %(student)s
		ORDER BY academic_year DESC, academic_term DESC
		""",
		{"student": student},
		as_dict=True,
	)

	record = []
	for p in periods:
		term = term_grades(
			student=student,
			academic_year=p.academic_year,
			academic_term=p.academic_term,
			persona=persona,
		)
		data = term.get("data") if isinstance(term, dict) and "success" in term else term
		# Skip a period the caller may not see a total for — a teacher's view
		# has no overall, and an unpublished term withholds one.
		if not data["subjects"]:
			continue
		record.append(
			{
				"academic_year": p.academic_year,
				"academic_term": p.academic_term,
				"subjects": data["subjects"],
				"overall": data["overall"],
				"overall_grade": data["overall_grade"],
				"published": data.get("published", False),
				"shows_overall": data.get("shows_overall", False),
			}
		)

	# The cumulative average is only meaningful over periods whose total the
	# caller is allowed to see.
	visible = [r["overall"] for r in record if r["shows_overall"] and r["overall"] is not None]
	cumulative = round(sum(visible) / len(visible), 1) if visible else None

	student_doc = frappe.db.get_value(
		"Student", student, ["student_name", "image"], as_dict=True
	) or {}

	return {
		"student": student,
		"student_name": student_doc.get("student_name"),
		"image": student_doc.get("image"),
		"periods": record,
		"cumulative": cumulative,
		"cumulative_grade": grade_for(cumulative) if cumulative is not None else None,
		"shows_cumulative": cumulative is not None,
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def class_term_grades(
	student_group: str,
	course: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Term grades for a whole class — the teacher's overview."""
	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name"],
		order_by="group_roll_number, student_name",
	)
	group = frappe.db.get_value(
		"Student Group", student_group, ["academic_year", "academic_term"], as_dict=True
	)
	academic_year = group.academic_year if group else get_default_academic_year()
	academic_term = academic_term or (group.academic_term if group else None)

	rows = []
	for s in roster:
		filters = {"student": s.student, "academic_year": academic_year}
		if academic_term:
			filters["academic_term"] = academic_term
		if course:
			filters["course"] = course

		entries = frappe.get_all(
			"K12 Gradebook Entry",
			filters=filters,
			fields=["course", "score", "max_score", "weight", "is_bonus"],
		)
		if course:
			computed = _compute_subject_grade(entries)
		else:
			by_course: dict[str, list] = {}
			for e in entries:
				by_course.setdefault(e.course, []).append(e)
			per_subject = [_compute_subject_grade(v) for v in by_course.values()]
			average = (
				round(sum(p["final"] for p in per_subject) / len(per_subject), 1)
				if per_subject
				else 0.0
			)
			computed = {
				"percentage": average,
				"bonus": 0.0,
				"final": average,
				"covered": 100.0 if per_subject else 0.0,
				**grade_for(average),
			}

		rows.append(
			{
				"student": s.student,
				"student_name": s.student_name,
				"entries": len(entries),
				**computed,
			}
		)

	graded = [r for r in rows if r["entries"]]
	return {
		"student_group": student_group,
		"course": course,
		"academic_year": academic_year,
		"academic_term": academic_term,
		"rows": rows,
		"class_average": round(sum(r["final"] for r in graded) / len(graded), 1) if graded else 0.0,
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def import_exam_results(assessment_plan: str, weight: float = None, persona: str = None):
	"""Pull an exam's submitted results into the gradebook as one component."""
	plan = frappe.db.get_value(
		"Assessment Plan",
		assessment_plan,
		[
			"name", "assessment_name", "course", "student_group", "program",
			"academic_year", "academic_term", "maximum_assessment_score",
		],
		as_dict=True,
	)
	if not plan:
		return fail(message_en="Exam not found.", message_ar="لم يتم العثور على الامتحان.")

	results = frappe.get_all(
		"Assessment Result",
		filters={"assessment_plan": assessment_plan, "docstatus": 1},
		fields=["student", "student_name", "total_score"],
	)
	if not results:
		return fail(
			message_en="This exam has no submitted results yet.",
			message_ar="لا توجد نتائج معتمدة لهذا الامتحان بعد.",
		)

	return save_marks(
		payload={
			"student_group": plan.student_group,
			"course": plan.course,
			"academic_year": plan.academic_year,
			"academic_term": plan.academic_term,
			"assessment_plan": plan.name,
			"component_name": plan.assessment_name,
			"component_type": "Exam",
			"max_score": flt(plan.maximum_assessment_score) or 100,
			"weight": flt(weight),
			"marks": [
				{"student": r.student, "student_name": r.student_name, "score": flt(r.total_score)}
				for r in results
			],
		},
		persona=persona,
	)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def importable_assignments(
	student_group: str, course: str = None, academic_term: str = None, persona: str = None
):
	"""Graded assignments that can be carried into the term marks."""
	from match_k12.api.gradeflow import assert_teacher_owns_course

	if course:
		assert_teacher_owns_course(persona, course)

	filters = {"student_group": student_group}
	if course:
		filters["course"] = course

	rows = frappe.get_all(
		"K12 Assignment",
		filters=filters,
		fields=["name", "title", "course", "maximum_score", "due_date", "status"],
		order_by="due_date desc",
		limit=100,
	)

	roster = frappe.db.count(
		"Student Group Student",
		{"parent": student_group, "parenttype": "Student Group", "active": 1},
	)

	out = []
	for r in rows:
		if persona == ROLE_TEACHER:
			from match_k12.api.gradeflow import teacher_may_see_course

			if not teacher_may_see_course(persona, r.course):
				continue
		graded = frappe.db.count(
			"K12 Assignment Submission",
			{"assignment": r.name, "status": ["in", ["Graded", "Returned"]]},
		)
		# Whether this assignment has already been carried across.
		imported = frappe.db.exists(
			"K12 Gradebook Entry",
			{
				"student_group": student_group,
				"course": r.course,
				"component_name": _assignment_component_name(r.title),
			},
		)
		out.append(
			{
				"id": r.name,
				"title": r.title,
				"course": r.course,
				"max": flt(r.maximum_score),
				"due": str(r.due_date or ""),
				"status": r.status,
				"graded": graded,
				"total": roster,
				"ready": graded > 0,
				"imported": bool(imported),
			}
		)
	return out


def _assignment_component_name(title: str) -> str:
	"""A stable component name, so re-importing updates rather than duplicates."""
	return f"واجب: {title}"


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def import_assignment(
	assignment: str,
	weight: float = None,
	component_name: str = None,
	persona: str = None,
):
	"""Carry one assignment's marks into the term gradebook.

	Ungraded students are skipped rather than scored zero — a missing mark is
	not the same as a zero, and the teacher may still be marking.
	"""
	from match_k12.api.gradeflow import assert_teacher_owns_course

	doc = frappe.db.get_value(
		"K12 Assignment",
		assignment,
		["name", "title", "course", "student_group", "program", "maximum_score"],
		as_dict=True,
	)
	if not doc:
		return fail(message_en="Assignment not found.", message_ar="لم يتم العثور على الواجب.")

	assert_teacher_owns_course(persona, doc.course)

	results = frappe.get_all(
		"K12 Assignment Submission",
		filters={"assignment": assignment, "status": ["in", ["Graded", "Returned"]]},
		fields=["student", "score"],
	)
	if not results:
		return fail(
			message_en="No graded submissions to carry across yet.",
			message_ar="لا توجد تسليمات مُصححة لترحيلها بعد.",
		)

	group = frappe.db.get_value(
		"Student Group",
		doc.student_group,
		["academic_year", "academic_term"],
		as_dict=True,
	)

	return save_marks(
		payload={
			"student_group": doc.student_group,
			"course": doc.course,
			"academic_year": group.academic_year if group else None,
			"academic_term": group.academic_term if group else None,
			"component_name": component_name or _assignment_component_name(doc.title),
			"component_type": "Homework",
			"max_score": flt(doc.maximum_score) or 100,
			"weight": flt(weight),
			"marks": [
				{"student": r.student, "score": flt(r.score)}
				for r in results
				if r.score is not None
			],
		},
		persona=persona,
	)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def import_assignments_combined(
	student_group: str,
	course: str,
	assignments: str | list,
	component_name: str = None,
	weight: float = None,
	persona: str = None,
):
	"""Carry several assignments across as a single averaged component.

	Schools usually want one "الواجبات" line in the term marks rather than one
	row per assignment, so each student's assignments are averaged as a
	percentage first.
	"""
	from match_k12.api.gradeflow import assert_teacher_owns_course

	assert_teacher_owns_course(persona, course)

	names = parse_json_arg(assignments, []) or []
	if isinstance(names, str):
		names = [names]
	if not names:
		return fail(
			message_en="Choose at least one assignment.",
			message_ar="اختر واجباً واحداً على الأقل.",
		)

	maxima = {
		r.name: flt(r.maximum_score) or 100
		for r in frappe.get_all(
			"K12 Assignment",
			filters={"name": ["in", names], "student_group": student_group, "course": course},
			fields=["name", "maximum_score"],
		)
	}
	if not maxima:
		return fail(
			message_en="Those assignments do not belong to this class and subject.",
			message_ar="الواجبات المختارة لا تخص هذه الشعبة والمادة.",
		)

	ratios: dict[str, list[float]] = {}
	for r in frappe.get_all(
		"K12 Assignment Submission",
		filters={
			"assignment": ["in", list(maxima)],
			"status": ["in", ["Graded", "Returned"]],
		},
		fields=["student", "assignment", "score"],
	):
		if r.score is None:
			continue
		top = maxima.get(r.assignment) or 100
		ratios.setdefault(r.student, []).append(flt(r.score) / top * 100)

	if not ratios:
		return fail(
			message_en="No graded submissions to carry across yet.",
			message_ar="لا توجد تسليمات مُصححة لترحيلها بعد.",
		)

	group = frappe.db.get_value(
		"Student Group", student_group, ["academic_year", "academic_term"], as_dict=True
	)

	return save_marks(
		payload={
			"student_group": student_group,
			"course": course,
			"academic_year": group.academic_year if group else None,
			"academic_term": group.academic_term if group else None,
			"component_name": component_name or "الواجبات",
			"component_type": "Homework",
			"max_score": 100,
			"weight": flt(weight),
			"marks": [
				{"student": student, "score": round(sum(vals) / len(vals), 1)}
				for student, vals in ratios.items()
			],
		},
		persona=persona,
	)
