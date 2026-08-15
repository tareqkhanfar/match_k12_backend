# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Quarters, tree-shaped assessment plans, and how a teacher counts marks.

A school divides a term into quarters — Q1 worth 40 marks, Q2 worth 60 — and
writes each subject's plan against them. Inside a quarter the plan is a tree,
two levels deep:

    Q1  (40 marks)
    └── امتحانات يومية        20%      ← a category; its weight reaches the 100
        ├── امتحان يومي ١     10 marks ← an assessment inside it
        ├── امتحان يومي ٢     10 marks
        └── امتحان يومي ٣     10 marks

Only the category's weight counts toward the quarter. The assessments beneath
it are averaged into that weight, which is what lets a school add a fourth
daily test in week ten without redoing the plan.

**How the children are combined is not part of the plan.** A teacher decides
during the term — "count the best three of four" — because it depends on how
the term actually went. That rule lives in `MS Grade Rule`, is set by the
teacher who owns the course, and recalculating is explicit so a class never
sees marks shift without someone deciding they should.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	ms_endpoint,
	parse_json_arg,
)

# How a category's assessments are combined into its weight.
COUNT_MODES = {
	"all": "احتساب الجميع",
	"best": "أعلى N",
	"drop_lowest": "استبعاد الأدنى",
}


# --- Quarters --------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_quarters(academic_term: str = None, persona: str = None):
	"""The parts this term is divided into, and what each is worth."""
	return _read_quarters(academic_term=academic_term, persona=persona)


def _read_quarters(academic_term: str = None, persona: str = None) -> dict:
	"""The term's quarters, with no persona gate — see `_read_plan`."""
	academic_term = academic_term or get_default_academic_term()
	if not academic_term:
		return {"academicTerm": None, "quarters": [], "total": 0}

	doc = frappe.get_doc("Academic Term", academic_term)
	quarters = [
		{
			"name": q.quarter_name,
			"totalMarks": flt(q.total_marks),
			"from": str(q.from_date or ""),
			"to": str(q.to_date or ""),
			"idx": cint(q.idx),
		}
		for q in (doc.get("ms_quarters") or [])
	]
	return {
		"academicTerm": academic_term,
		"termName": doc.term_name or academic_term,
		"quarters": quarters,
		"total": sum(q["totalMarks"] for q in quarters),
		"canEdit": persona in BACK_OFFICE,
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_quarters(academic_term: str = None, quarters: str | list = None, persona: str = None):
	"""Define how a term is divided.

	The marks are not forced to sum to 100: a school may run a term out of 200,
	and the plan works from whatever total is set here. What is refused is a
	quarter with no name, a non-positive total, or two quarters sharing a name —
	each of those would make a plan ambiguous later.
	"""
	academic_term = academic_term or get_default_academic_term()
	if not academic_term:
		return fail(
			message_en="An academic term is required.",
			message_ar="يجب تحديد الفصل الدراسي.",
		)

	rows = parse_json_arg(quarters, []) or []
	if not rows:
		return fail(
			message_en="At least one quarter is required.",
			message_ar="يجب تعريف ربع واحد على الأقل.",
		)

	seen: set[str] = set()
	cleaned = []
	for row in rows:
		name = (row.get("name") or row.get("quarter_name") or "").strip()
		if not name:
			return fail(
				message_en="Every quarter needs a name.",
				message_ar="يجب تسمية كل ربع.",
			)
		if name in seen:
			return fail(
				message_en=f"Duplicate quarter name: {name}.",
				message_ar=f"اسم الربع مكرر: {name}.",
			)
		seen.add(name)

		total = flt(row.get("totalMarks") if "totalMarks" in row else row.get("total_marks"))
		if total <= 0:
			return fail(
				message_en=f"{name}: total marks must be greater than zero.",
				message_ar=f"{name}: مجموع العلامات يجب أن يكون أكبر من صفر.",
			)
		cleaned.append(
			{
				"quarter_name": name,
				"total_marks": total,
				"from_date": row.get("from") or row.get("from_date") or None,
				"to_date": row.get("to") or row.get("to_date") or None,
			}
		)

	doc = frappe.get_doc("Academic Term", academic_term)
	# A quarter that plans already reference must not silently disappear.
	existing = {q.quarter_name for q in (doc.get("ms_quarters") or [])}
	removed = existing - seen
	if removed:
		in_use = frappe.get_all(
			"MS Grade Scheme Component",
			filters={"ms_quarter": ["in", list(removed)], "parenttype": "MS Grade Scheme"},
			pluck="name",
			limit=1,
		)
		if in_use:
			return fail(
				message_en="A quarter in use by an assessment plan cannot be removed.",
				message_ar=(
					"لا يمكن حذف ربع مستخدم في خطة تقييم: "
					+ "، ".join(sorted(removed))
				),
			)

	doc.set("ms_quarters", [])
	for row in cleaned:
		doc.append("ms_quarters", row)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"academicTerm": academic_term,
		"quarters": len(cleaned),
		"total": sum(r["total_marks"] for r in cleaned),
		"message_ar": f"تم حفظ {len(cleaned)} أرباع للفصل.",
	}


# --- The plan --------------------------------------------------------------


def _tree(components: list) -> list[dict]:
	"""Group flat component rows into categories with their assessments."""
	categories: dict[str, dict] = {}
	orphans: list[dict] = []

	for c in components:
		if c.get("ms_parent_component"):
			continue
		categories[c["component_name"]] = {
			"name": c["component_name"],
			"type": c.get("component_type"),
			"quarter": c.get("ms_quarter"),
			"weight": flt(c.get("weight")),
			"maxScore": flt(c.get("max_score")),
			"children": [],
		}

	for c in components:
		parent = c.get("ms_parent_component")
		if not parent:
			continue
		child = {
			"name": c["component_name"],
			"type": c.get("component_type"),
			"maxScore": flt(c.get("max_score")),
			"parent": parent,
		}
		if parent in categories:
			categories[parent]["children"].append(child)
		else:
			# A category renamed without its children following it. Surfaced
			# rather than dropped, so the plan screen can point at the problem.
			orphans.append(child)

	return list(categories.values()), orphans


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_plan(course: str = None, program: str = None, academic_term: str = None, persona: str = None):
	"""One subject's plan, as quarters holding categories holding assessments.

	Editing a plan is back-office work, which is what this endpoint guards.
	Reading one in order to total a student's own marks is not, so the body
	lives in `_read_plan()` and is called directly by the calculation.
	"""
	return _read_plan(course=course, program=program, academic_term=academic_term, persona=persona)


def _read_plan(
	course: str = None, program: str = None, academic_term: str = None, persona: str = None
) -> dict:
	"""The plan itself, with no persona gate.

	Deliberately not an endpoint: a student totalling their own subject has to
	read the plan that defines it. The caller is responsible for having
	established that this student may see these marks — `term_grades` does so
	through `_assert_can_see` before it ever gets here.
	"""
	academic_term = academic_term or get_default_academic_term()

	filters = {"course": course}
	if program:
		filters["program"] = program
	if academic_term:
		filters["academic_term"] = academic_term

	scheme = frappe.db.get_value(
		"MS Grade Scheme", filters, ["name", "scheme_name"], as_dict=True
	)
	quarters_info = _read_quarters(academic_term=academic_term, persona=persona)
	quarters = (quarters_info.get("data") or quarters_info).get("quarters", [])

	components = []
	if scheme:
		components = frappe.get_all(
			"MS Grade Scheme Component",
			filters={"parent": scheme.name, "parenttype": "MS Grade Scheme"},
			fields=[
				"component_name", "component_type", "weight", "max_score",
				"ms_parent_component", "ms_quarter", "idx",
			],
			order_by="idx",
			limit_page_length=0,
		)

	categories, orphans = _tree(components)

	by_quarter = []
	for q in quarters:
		mine = [c for c in categories if (c["quarter"] or "") == q["name"]]
		by_quarter.append(
			{
				**q,
				"categories": mine,
				"weightUsed": round(sum(c["weight"] for c in mine), 2),
				# Complete when the categories add up to the quarter's own
				# marks — 40 for a 40-mark quarter, not 100.
				"balanced": (
					abs(sum(c["weight"] for c in mine) - flt(q.get("totalMarks"))) < 0.01
					if flt(q.get("totalMarks"))
					else False
				),
			}
		)

	# Categories written before quarters existed, or against a removed one.
	unassigned = [c for c in categories if not c["quarter"]]

	return {
		"course": course,
		"program": program,
		"academicTerm": academic_term,
		"scheme": scheme.name if scheme else None,
		"schemeName": scheme.scheme_name if scheme else None,
		"quarters": by_quarter,
		"unassigned": unassigned,
		"orphans": orphans,
		"canEdit": persona in BACK_OFFICE or persona == ROLE_TEACHER,
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_plan(
	course: str = None,
	program: str = None,
	academic_term: str = None,
	scheme_name: str = None,
	categories: str | list = None,
	persona: str = None,
):
	"""Write a subject's plan.

	`categories` is a list of {quarter, name, type, weight, children:[{name,
	maxScore}]}. Validation is strict because a plan that does not add up
	produces marks nobody can explain:

	  * every category belongs to a quarter that exists
	  * category weights sum to 100 within each quarter
	  * names are unique inside a quarter, and inside a category
	  * an assessment carries a positive maximum
	"""
	if not course:
		return fail(
			message_en="A course is required.",
			message_ar="يجب تحديد المادة.",
		)

	academic_term = academic_term or get_default_academic_term()
	rows = parse_json_arg(categories, []) or []
	if not rows:
		return fail(
			message_en="At least one category is required.",
			message_ar="يجب تعريف تصنيف واحد على الأقل.",
		)

	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import assert_teacher_owns_course

		assert_teacher_owns_course(persona, course)

	quarters_info = get_quarters(academic_term=academic_term, persona=persona)
	quarters = (quarters_info.get("data") or quarters_info).get("quarters", [])
	quarter_names = {q["name"] for q in quarters}
	if not quarter_names:
		return fail(
			message_en="Define the term's quarters before writing a plan.",
			message_ar="يجب تعريف أرباع الفصل قبل إنشاء خطة التقييم.",
		)

	problems: list[str] = []
	weight_by_quarter: dict[str, float] = {}
	names_by_quarter: dict[str, set] = {}
	# Every assessment name in the plan and the quarter it came from, so a
	# name reused in another quarter can be reported with both locations.
	assessment_names: dict[str, str] = {}

	for cat in rows:
		name = (cat.get("name") or "").strip()
		quarter = (cat.get("quarter") or "").strip()
		weight = flt(cat.get("weight"))

		if not name:
			problems.append("تصنيف بدون اسم")
			continue
		if quarter not in quarter_names:
			problems.append(f"{name}: الربع «{quarter}» غير معرّف في هذا الفصل")
			continue
		if weight <= 0:
			problems.append(f"{name}: الوزن يجب أن يكون أكبر من صفر")
		seen = names_by_quarter.setdefault(quarter, set())
		if name in seen:
			problems.append(f"{name}: التصنيف مكرر في {quarter}")
		seen.add(name)
		weight_by_quarter[quarter] = weight_by_quarter.get(quarter, 0) + weight

		child_names: set[str] = set()
		for child in cat.get("children") or []:
			cname = (child.get("name") or "").strip()
			if not cname:
				problems.append(f"{name}: امتحان بدون اسم")
				continue
			if cname in child_names:
				problems.append(f"{name}: «{cname}» مكرر داخل التصنيف")
			child_names.add(cname)

			# A mark is filed against (course, component_name) — the quarter is
			# not part of the key. So the same assessment name used in two
			# quarters produced one duplicated row in the marks screen and no
			# way to tell which quarter a score belonged to. Names must be
			# unique across the whole plan, not just within a category.
			if cname in assessment_names:
				problems.append(
					f"«{cname}» مستخدم مرتين في الخطة "
					f"({assessment_names[cname]} و {quarter}) — "
					"لا يمكن تكرار اسم الامتحان لأن العلامات تُحفظ بالاسم."
				)
			else:
				assessment_names[cname] = quarter

			if flt(child.get("maxScore")) <= 0:
				problems.append(f"{cname}: العلامة العظمى يجب أن تكون أكبر من صفر")

	# A category's weight is written in the quarter's own marks, not as a
	# percentage: a quarter worth 40 has categories summing to 40. Demanding
	# 100 everywhere forced the teacher to convert in their head and made the
	# 40/60 split meaningless.
	marks_by_quarter = {q["name"]: flt(q["totalMarks"]) for q in quarters}
	for quarter, total in weight_by_quarter.items():
		expected = marks_by_quarter.get(quarter, 0)
		if expected and abs(total - expected) >= 0.01:
			problems.append(
				f"{quarter}: مجموع علامات التصنيفات {round(total, 2)} — "
				f"يجب أن يكون {round(expected, 2)}"
			)

	if problems:
		return fail(
			message_en=f"{len(problems)} problem(s) in the plan. Nothing was saved.",
			message_ar="لم يتم حفظ الخطة. صحّح ما يلي:\n" + "\n".join(f"• {p}" for p in problems[:8]),
			data={"problems": problems},
		)

	filters = {"course": course}
	if program:
		filters["program"] = program
	if academic_term:
		filters["academic_term"] = academic_term
	existing = frappe.db.get_value("MS Grade Scheme", filters, "name")

	doc = frappe.get_doc("MS Grade Scheme", existing) if existing else frappe.new_doc("MS Grade Scheme")
	doc.course = course
	if program:
		doc.program = program
	if academic_term:
		doc.academic_term = academic_term
		doc.academic_year = frappe.db.get_value("Academic Term", academic_term, "academic_year")
	doc.scheme_name = scheme_name or f"خطة {course}"

	doc.set("components", [])
	for cat in rows:
		doc.append(
			"components",
			{
				"component_name": cat["name"].strip(),
				"component_type": cat.get("type") or "Exam",
				"weight": flt(cat.get("weight")),
				# A category with assessments inside it is never marked
				# directly, so its own maximum is unused. One without them is
				# marked directly, and the plan editor offers a single figure
				# for it — "العلامة" — which is both what it is worth and what
				# it is out of. Defaulting to 100 made the sheet invite a mark
				# of 90 against a category the plan says is worth 20, and made
				# a 40-mark quarter read as 245.
				"max_score": (
					flt(cat.get("maxScore"))
					or (flt(cat.get("weight")) if not (cat.get("children") or []) else 100)
					or 100
				),
				"ms_quarter": (cat.get("quarter") or "").strip(),
				"ms_parent_component": None,
			},
		)
		for child in cat.get("children") or []:
			doc.append(
				"components",
				{
					"component_name": child["name"].strip(),
					"component_type": child.get("type") or cat.get("type") or "Exam",
					# A child carries no weight of its own: the category's weight
					# is what reaches the final mark.
					"weight": 0,
					"max_score": flt(child.get("maxScore")),
					"ms_quarter": (cat.get("quarter") or "").strip(),
					"ms_parent_component": cat["name"].strip(),
				},
			)

	doc.total_weight = sum(flt(c.get("weight")) for c in rows)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"scheme": doc.name,
		"categories": len(rows),
		"assessments": sum(len(c.get("children") or []) for c in rows),
		"message_ar": "تم حفظ خطة التقييم.",
	}


# --- How a teacher counts a category ---------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_rules(student_group: str = None, course: str = None, academic_term: str = None, persona: str = None):
	"""The counting rules this class currently uses."""
	return _read_rules(
		student_group=student_group, course=course, academic_term=academic_term, persona=persona
	)


def _read_rules(
	student_group: str = None, course: str = None, academic_term: str = None, persona: str = None
) -> dict:
	"""The counting rules, with no persona gate — see `_read_plan`."""
	if not student_group or not course:
		return fail(
			message_en="A class and course are required.",
			message_ar="يجب تحديد الشعبة والمادة.",
		)

	academic_term = academic_term or get_default_academic_term()
	filters = {"student_group": student_group, "course": course}
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"MS Grade Rule",
		filters=filters,
		fields=["name", "quarter", "category", "count_mode", "count_n", "set_by", "set_on", "notes"],
		limit_page_length=0,
	)
	return {
		"rules": [
			{
				"id": r.name,
				"quarter": r.quarter,
				"category": r.category,
				"mode": r.count_mode,
				"modeLabel": COUNT_MODES.get(r.count_mode, r.count_mode),
				"n": cint(r.count_n),
				"setBy": r.set_by,
				"setOn": str(r.set_on or ""),
				"notes": r.notes,
			}
			for r in rows
		],
		"modes": [{"value": k, "label": v} for k, v in COUNT_MODES.items()],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_rule(
	student_group: str = None,
	course: str = None,
	category: str = None,
	quarter: str = None,
	count_mode: str = "all",
	count_n: int = 0,
	academic_term: str = None,
	notes: str = None,
	persona: str = None,
):
	"""Set how one category's assessments are counted for this class.

	Refused when the rule cannot be satisfied — "best 5 of 3" is a mistake that
	would otherwise quietly behave like "all" and nobody would notice until a
	parent queried a mark.
	"""
	if not student_group or not course or not category:
		return fail(
			message_en="A class, course and category are required.",
			message_ar="يجب تحديد الشعبة والمادة والتصنيف.",
		)
	if count_mode not in COUNT_MODES:
		return fail(
			message_en="Unknown counting mode.",
			message_ar="طريقة احتساب غير معروفة.",
		)

	academic_term = academic_term or get_default_academic_term()

	from match_schools.api.gradeflow import assert_entry_allowed, assert_teacher_owns_course

	assert_teacher_owns_course(persona, course)
	assert_entry_allowed(persona, student_group, course, academic_term)

	# How many assessments the plan actually defines under this category.
	available = _assessments_of(course, category, academic_term)
	n = cint(count_n)

	if count_mode == "best":
		if n <= 0:
			return fail(
				message_en="Choose how many assessments count.",
				message_ar="يجب تحديد عدد الامتحانات التي ستُحتسب.",
			)
		if available and n > len(available):
			return fail(
				message_en=f"Only {len(available)} assessments exist in this category.",
				message_ar=(
					f"لا يوجد سوى {len(available)} امتحان في «{category}» — "
					f"لا يمكن احتساب أعلى {n}."
				),
			)
	if count_mode == "drop_lowest" and available and len(available) < 2:
		return fail(
			message_en="At least two assessments are needed to drop the lowest.",
			message_ar="يلزم امتحانان على الأقل لاستبعاد الأدنى.",
		)

	filters = {"student_group": student_group, "course": course, "category": category}
	if academic_term:
		filters["academic_term"] = academic_term
	existing = frappe.db.get_value("MS Grade Rule", filters, "name")

	doc = frappe.get_doc("MS Grade Rule", existing) if existing else frappe.new_doc("MS Grade Rule")
	doc.student_group = student_group
	doc.course = course
	doc.category = category
	doc.quarter = quarter
	doc.academic_term = academic_term
	doc.count_mode = count_mode
	doc.count_n = n
	doc.notes = notes
	doc.set_by = frappe.session.user
	doc.set_on = frappe.utils.now()
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"id": doc.name,
		"message_ar": (
			f"تم ضبط احتساب «{category}»: {COUNT_MODES[count_mode]}"
			+ (f" ({n})" if count_mode == "best" else "")
			+ "."
		),
	}


def _assessments_of(course: str, category: str, academic_term: str | None) -> list[dict]:
	"""The assessments the plan defines inside one category."""
	filters = {"course": course}
	if academic_term:
		filters["academic_term"] = academic_term
	scheme = frappe.db.get_value("MS Grade Scheme", filters, "name")
	if not scheme:
		return []
	return frappe.get_all(
		"MS Grade Scheme Component",
		filters={
			"parent": scheme,
			"parenttype": "MS Grade Scheme",
			"ms_parent_component": category,
		},
		fields=["component_name", "max_score"],
		order_by="idx",
		limit_page_length=0,
	)


def _apply_rule(scores: list[dict], mode: str, n: int) -> tuple[list[dict], list[dict]]:
	"""Split assessments into the ones that count and the ones dropped.

	A missing mark is treated as zero, as the school asked: a student who never
	sat a test has not earned its marks. The rule is applied to the percentage
	each assessment is worth, so a 10-mark quiz and a 20-mark test compare
	fairly.
	"""
	if mode == "all" or not scores:
		return scores, []

	ranked = sorted(scores, key=lambda s: s["percent"], reverse=True)

	if mode == "best":
		keep = ranked[: max(n, 0)]
		drop = ranked[max(n, 0) :]
	elif mode == "drop_lowest":
		keep = ranked[:-1] if len(ranked) > 1 else ranked
		drop = ranked[-1:] if len(ranked) > 1 else []
	else:
		keep, drop = scores, []

	return keep, drop


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def compute_marks(
	student_group: str = None,
	course: str = None,
	academic_term: str = None,
	student: str = None,
	persona: str = None,
):
	"""Work a class's marks through the plan, showing every step.

	Returns the arithmetic, not just the answer: which assessments counted,
	which were dropped by the teacher's rule, what each category came to, and
	how the quarters combine. A teacher explaining a mark to a parent should be
	able to read it off the screen.

	Missing assessments count as zero — the school's rule — so a mark only
	rises when the work is actually done.
	"""
	if not student_group or not course:
		return fail(
			message_en="A class and course are required.",
			message_ar="يجب تحديد الشعبة والمادة.",
		)

	academic_term = academic_term or get_default_academic_term()

	# The ungated readers: a student computing their own subject must be able to
	# read the plan behind it. Going through the endpoints would raise
	# PermissionError here and silently fall back to a flat average — which is
	# exactly how the portal came to show 78.9 for a subject worth 84.5.
	plan_res = _read_plan(course=course, academic_term=academic_term, persona=persona)
	plan = plan_res.get("data") if isinstance(plan_res, dict) and "data" in plan_res else plan_res
	quarters = plan.get("quarters") or []
	if not quarters:
		return fail(
			message_en="No assessment plan is defined for this course.",
			message_ar="لا توجد خطة تقييم لهذه المادة في هذا الفصل.",
		)

	rules_res = _read_rules(
		student_group=student_group, course=course, academic_term=academic_term, persona=persona
	)
	rules_data = rules_res.get("data") if isinstance(rules_res, dict) and "data" in rules_res else rules_res
	rule_by_category = {r["category"]: r for r in (rules_data or {}).get("rules", [])}

	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group"},
		fields=["student", "student_name"],
		order_by="idx",
		limit_page_length=0,
	)
	if student:
		roster = [r for r in roster if r.student == student]
	if not roster:
		return {"students": [], "quarters": quarters}

	entry_filters = {
		"student_group": student_group,
		"course": course,
		"student": ["in", [r.student for r in roster]],
	}
	if academic_term:
		entry_filters["academic_term"] = academic_term

	entries = frappe.get_all(
		"MS Gradebook Entry",
		filters=entry_filters,
		fields=["student", "component_name", "score", "max_score"],
		limit_page_length=0,
	)
	marks: dict[tuple, dict] = {}
	for e in entries:
		marks[(e.student, e.component_name)] = e

	results = []
	for r in roster:
		quarter_results = []
		for q in quarters:
			category_results = []
			for cat in q.get("categories") or []:
				children = cat.get("children") or []
				rule = rule_by_category.get(cat["name"]) or {"mode": "all", "n": 0}

				if children:
					scored = []
					for child in children:
						entry = marks.get((r.student, child["name"]))
						maximum = flt(child.get("maxScore")) or flt(
							entry.max_score if entry else 0
						)
						# No entry means the assessment was not sat: zero.
						value = flt(entry.score) if entry else 0.0
						scored.append(
							{
								"name": child["name"],
								"score": value,
								"maxScore": maximum,
								"percent": (value / maximum * 100) if maximum else 0.0,
								"missing": entry is None,
							}
						)
					kept, dropped = _apply_rule(scored, rule.get("mode", "all"), cint(rule.get("n")))
					percent = (sum(s["percent"] for s in kept) / len(kept)) if kept else 0.0
				else:
					# A category with no assessments beneath it is marked
					# directly — the flat plans that predate the tree.
					entry = marks.get((r.student, cat["name"]))
					maximum = flt(cat.get("maxScore")) or flt(entry.max_score if entry else 0)
					value = flt(entry.score) if entry else 0.0
					percent = (value / maximum * 100) if maximum else 0.0
					kept = [
						{
							"name": cat["name"],
							"score": value,
							"maxScore": maximum,
							"percent": percent,
							"missing": entry is None,
						}
					]
					dropped = []

				# `weight` is the category's marks within the quarter — 8 of a
				# 40-mark quarter, not 8%. So the marks earned are that many
				# multiplied by how much of the category the student achieved.
				weight = flt(cat.get("weight"))
				category_results.append(
					{
						"category": cat["name"],
						"weight": weight,
						"percent": round(percent, 2),
						# Marks earned out of this category's own marks.
						"earned": round(percent * weight / 100, 2),
						"rule": rule.get("mode", "all"),
						"ruleLabel": COUNT_MODES.get(rule.get("mode", "all")),
						"ruleN": cint(rule.get("n")),
						"counted": kept,
						"dropped": dropped,
					}
				)

			# Categories are already in marks, so the quarter's mark is their
			# sum. Scaling again by totalMarks/100 — as an earlier version did —
			# turned 8 marks out of 40 into 3.2.
			quarter_marks = sum(c["earned"] for c in category_results)
			quarter_total = flt(q.get("totalMarks"))
			quarter_results.append(
				{
					"quarter": q["name"],
					"totalMarks": quarter_total,
					"percent": round(quarter_marks / quarter_total * 100, 2) if quarter_total else 0.0,
					"marks": round(quarter_marks, 2),
					"categories": category_results,
				}
			)

		total_marks = sum(flt(q.get("totalMarks")) for q in quarters) or 100
		earned_marks = sum(qr["marks"] for qr in quarter_results)
		results.append(
			{
				"student": r.student,
				"studentName": r.student_name,
				"quarters": quarter_results,
				"marks": round(earned_marks, 2),
				"totalMarks": total_marks,
				"percent": round(earned_marks / total_marks * 100, 2) if total_marks else 0.0,
			}
		)

	return {
		"students": results,
		"quarters": [
			{"name": q["name"], "totalMarks": flt(q.get("totalMarks"))} for q in quarters
		],
		"rules": list(rule_by_category.values()),
		"course": course,
		"studentGroup": student_group,
		"academicTerm": academic_term,
	}
