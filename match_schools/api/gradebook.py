# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The gradebook: mark entry, term grades and the student's academic record.

A subject's final mark is built from weighted components — exams, quizzes,
activities, homework — defined by a MS Grade Scheme. Bonus components sit
outside the 100% and are added on top, capped so a student cannot exceed full
marks.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate, today

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
		from match_schools.api.students import _students_of_instructor

		if student not in _students_of_instructor(scope.get("instructor")):
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)
		return
	if student not in (scope.get("students") or []):
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)


# --- Grade schemes ---------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def list_schemes(course: str = None, program: str = None, persona: str = None):
	filters = {}
	if course:
		filters["course"] = course
	if program:
		filters["program"] = program

	rows = frappe.get_all(
		"MS Grade Scheme",
		filters=filters,
		fields=[
			"name", "scheme_name", "course", "program", "academic_year",
			"academic_term", "is_default", "total_weight",
		],
		order_by="scheme_name",
	)
	for r in rows:
		r["components"] = frappe.get_all(
			"MS Grade Scheme Component",
			filters={"parent": r["name"], "parenttype": "MS Grade Scheme"},
			fields=["component_name", "component_type", "weight", "max_score"],
			order_by="idx",
		)
		for c in r["components"]:
			c["type_label"] = COMPONENT_TYPE_AR.get(c["component_type"], c["component_type"])
		r["id"] = r["name"]
	return rows


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_scheme(payload: str | dict, persona: str = None):
	"""Create or update a weighting scheme.

	A teacher may adjust the plan for a subject they teach — the weighting is
	a teaching decision, and marks are never validated against it anyway.
	They cannot touch a school-wide default.
	"""
	from match_schools.api.gradeflow import assert_teacher_owns_course

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
		frappe.get_doc("MS Grade Scheme", scheme_id)
		if scheme_id
		else frappe.new_doc("MS Grade Scheme")
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_scheme(scheme: str, persona: str = None):
	from match_schools.api.gradeflow import assert_teacher_owns_course

	doc = frappe.db.get_value(
		"MS Grade Scheme", scheme, ["course", "is_default"], as_dict=True
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

	frappe.delete_doc("MS Grade Scheme", scheme)
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
		name = frappe.db.get_value("MS Grade Scheme", clean, "name")
		if name:
			doc = frappe.get_doc("MS Grade Scheme", name)
			# A plan is a tree: categories carry the weight, and the individual
			# assessments sit beneath them. Marks are entered against the
			# assessments — a category with children is a total, not something
			# a teacher scores directly. A category with no children is still
			# marked directly, which is how the flat plans that predate the
			# tree keep working.
			children_of: dict[str, list] = {}
			for c in doc.components:
				parent = c.get("ms_parent_component")
				if parent:
					children_of.setdefault(parent, []).append(c)

			markable = []
			# A mark is stored against (course, component_name), so two
			# assessments sharing a name are the same cell as far as the
			# gradebook is concerned. Offering both listed the subject twice
			# and left the sheet unable to say which one was being marked.
			# Plans saved from now on refuse duplicate names outright; this
			# keeps the screen usable on plans built before that check.
			seen_names: set[str] = set()
			for c in doc.components:
				if c.component_name in seen_names:
					continue
				if c.get("ms_parent_component"):
					markable.append(c)
					seen_names.add(c.component_name)
				elif not children_of.get(c.component_name):
					markable.append(c)
					seen_names.add(c.component_name)

			# The plan's own shape travels with it. The sheet needs to know
			# which assessments roll up into which heading, and what that
			# heading is worth, so it can total a parent from its children and
			# show the arithmetic — none of which is recoverable from a flat
			# list of markable assessments.
			parents = []
			for c in doc.components:
				kids = children_of.get(c.component_name)
				if c.get("ms_parent_component") or not kids:
					continue
				parents.append(
					{
						"component_name": c.component_name,
						"component_type": c.component_type,
						"weight": flt(c.weight),
						"max_score": flt(c.max_score),
						"quarter": c.get("ms_quarter"),
						# What the children add up to, which is the figure a
						# teacher checks the heading against.
						"children_total": sum(flt(k.max_score) for k in kids),
						"children": [k.component_name for k in kids],
						"aggregation": c.get("ms_aggregation") or "sum",
						"aggregation_n": cint(c.get("ms_aggregation_n")),
					}
				)

			return {
				"id": doc.name,
				"scheme_name": doc.scheme_name,
				"parents": parents,
				"components": [
					{
						"component_name": c.component_name,
						"component_type": c.component_type,
						# An assessment inherits the weight of its category; the
						# category's own weight is what reaches the final mark.
						"weight": flt(c.weight),
						"max_score": flt(c.max_score),
						"quarter": c.get("ms_quarter"),
						"category": c.get("ms_parent_component"),
					}
					for c in markable
				],
			}
	return None


# --- Mark entry ------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
	from match_schools.api.gradeflow import (
		STATUS_AR,
		assert_teacher_teaches,
		submission_status,
	)

	assert_teacher_teaches(persona, student_group, course)

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
		"MS Gradebook Entry",
		filters=entry_filters,
		fields=[
			"name", "student", "component_name", "component_type",
			"score", "max_score", "weight", "is_bonus", "remarks",
			"ms_is_published", "ms_excluded", "ms_release_on",
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
						"excluded": bool(m.get("ms_excluded")),
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
		# Publication is per component. "Partly" happens when a component was
		# published and then extra marks were entered afterwards.
		"publishedCount": sum(
			1 for e in existing if cint(e.get("ms_is_published"))
		),
		"draftCount": sum(
			1 for e in existing if not cint(e.get("ms_is_published"))
		),
		# Per-column state and statistics. Worked out here because the mark
		# sheet shows one row of figures per assessment — average, highest,
		# lowest, how many are marked — and asking the browser to recompute
		# them for every keystroke on a class of forty is wasteful.
		"columns": _column_stats(scheme, existing, len(roster)),
		# The plan's headings, with what each is worth. The sheet totals a
		# heading from its children per student; the weight is what that
		# heading contributes to the subject mark.
		"parents": (scheme or {}).get("parents", []),
		# What each quarter is worth in total — the sum of the weights of the
		# headings that sit under it. Without this the sheet showed quarters
		# as bare labels with no indication of what they counted for.
		"quarter_totals": _quarter_totals(scheme),
	}


def _quarter_totals(scheme: dict | None) -> list[dict]:
	"""Weight and maximum per quarter, taken from what actually scores.

	A parent heading carries the weight for its children, so counting both
	would double it. Only headings and childless assessments are counted.
	"""
	if not scheme:
		return []
	owned: set[str] = set()
	for p in scheme.get("parents", []):
		owned.update(p.get("children") or [])

	# A quarter is worth the sum of its headings' weights, and nothing else.
	#
	# `weight` is the marks a heading contributes to the subject — the figure
	# the plan editor shows as "العلامة". `max_score` is what its own paper is
	# out of and defaults to 100 whether or not anyone meant it, so summing
	# max_score made a 40-mark quarter read as 245. Reporting the weight makes
	# the sheet say exactly what the plan says.
	by_quarter: dict[str, dict] = {}
	for p in scheme.get("parents", []):
		q = p.get("quarter") or ""
		row = by_quarter.setdefault(q, {"quarter": q, "weight": 0.0})
		row["weight"] += flt(p.get("weight"))
	for c in scheme.get("components", []):
		if c["component_name"] in owned:
			continue
		q = c.get("quarter") or ""
		row = by_quarter.setdefault(q, {"quarter": q, "weight": 0.0})
		row["weight"] += flt(c.get("weight"))

	return [
		{
			"quarter": k,
			"weight": round(v["weight"], 2),
			# The quarter is out of its own weight: 40 marks of the 100.
			"max_score": round(v["weight"], 2),
		}
		for k, v in by_quarter.items()
	]


def _column_stats(scheme: dict | None, entries: list, roster_size: int) -> list[dict]:
	"""One summary per assessment: how it was marked and where it stands.

	A teacher looking at a column wants to know whether the paper worked —
	an average of 40% says the paper was too hard far more clearly than
	thirty individual marks do.
	"""
	by_component: dict[str, list] = {}
	for e in entries:
		by_component.setdefault(e.component_name, []).append(e)

	out = []
	for c in (scheme or {}).get("components", []):
		name = c["component_name"]
		rows = by_component.get(name, [])
		scores = [flt(r.score) for r in rows]
		maximum = flt(c.get("max_score")) or 100

		published = sum(1 for r in rows if cint(r.get("ms_is_published")))
		out.append(
			{
				"component_name": name,
				"marked": len(rows),
				"missing": max(roster_size - len(rows), 0),
				"published": published,
				# Three states, not two: a column published and then added to
				# is neither fully out nor fully withheld.
				"publish_state": (
					"published"
					if rows and published == len(rows)
					else "partial"
					if published
					else "draft"
				),
				"excluded": bool(rows and all(cint(r.get("ms_excluded")) for r in rows)),
				"release_on": next(
					(str(r.get("ms_release_on")) for r in rows if r.get("ms_release_on")), None
				),
				"average": round(sum(scores) / len(scores), 2) if scores else None,
				"highest": round(max(scores), 2) if scores else None,
				"lowest": round(min(scores), 2) if scores else None,
				"average_pct": (
					round(sum(scores) / len(scores) / maximum * 100, 1) if scores and maximum else None
				),
			}
		)
	return out


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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

	from match_schools.api.gradeflow import assert_entry_allowed, assert_teacher_owns_course

	assert_teacher_owns_course(persona, data["course"])

	group = frappe.db.get_value(
		"Student Group",
		data["student_group"],
		["program", "academic_year", "academic_term"],
		as_dict=True,
	)
	academic_year = data.get("academic_year") or (group.academic_year if group else None) or get_default_academic_year()
	# The school's current term is the last resort, exactly as the year above
	# already did. A section with no term of its own used to store marks with
	# a null term while every screen reads by the active one, so a teacher
	# saved a sheet and got back an empty one — the marks were there, filed
	# under a term nothing matches.
	academic_term = (
		data.get("academic_term")
		or (group.academic_term if group else None)
		or get_default_academic_term()
	)

	# Once the marks are with the administration the teacher may not edit them.
	assert_entry_allowed(persona, data["student_group"], data["course"], academic_term)

	component_type = data.get("component_type") or "Exam"
	is_bonus = cint(data.get("is_bonus")) or (1 if component_type == "Bonus" else 0)
	max_score = flt(data.get("max_score")) or 100
	weight = flt(data.get("weight"))

	# --- Validate the whole batch before writing anything -----------------
	# Marks are a legal record. Saving the valid half of a sheet and reporting
	# the rest as "skipped" leaves the gradebook in a state nobody asked for,
	# so a single bad value refuses the entire save.
	problems: list[str] = []
	for row in marks:
		student = row.get("student")
		if not student:
			continue
		who = row.get("student_name") or student
		raw = row.get("score")

		if raw in (None, ""):
			problems.append(f"{who}: لم تُدخل علامة")
			continue
		try:
			value = flt(raw)
		except Exception:
			problems.append(f"{who}: قيمة غير صالحة")
			continue
		if value < 0:
			problems.append(f"{who}: العلامة سالبة")
			continue
		if not is_bonus and value > max_score:
			problems.append(f"{who}: {value} تتجاوز الحد الأقصى {max_score}")
			continue
		if (value * 2) % 1 != 0:
			problems.append(f"{who}: {value} ليست من مضاعفات ٠.٥")

	if problems:
		shown = problems[:5]
		more = len(problems) - len(shown)
		return fail(
			message_en=f"{len(problems)} invalid mark(s). Nothing was saved.",
			message_ar=(
				"لم يتم حفظ أي علامة. صحّح الأخطاء التالية:\n"
				+ "\n".join(f"• {p}" for p in shown)
				+ (f"\n• و{more} خطأ آخر" if more > 0 else "")
			),
			data={"problems": problems},
		)

	saved, updated, skipped = 0, 0, []
	rejected: list[dict] = []


	for row in marks:
		student = row.get("student")
		if not student:
			continue
		# An empty score means "not entered" — remove any previous value.
		raw = row.get("score")
		if raw in (None, ""):
			existing = frappe.db.get_value(
				"MS Gradebook Entry",
				{
					"student": student,
					"course": data["course"],
					"component_name": data["component_name"],
					"academic_year": academic_year,
				},
				"name",
			)
			if existing:
				frappe.delete_doc("MS Gradebook Entry", existing, ignore_permissions=True)
			continue

		score = flt(raw)
		if not is_bonus and score > max_score:
			skipped.append(row.get("student_name") or student)
			continue
		if score < 0:
			rejected.append(
				{"student": row.get("student_name") or student, "reason": "negative"}
			)
			continue
		# Marks are recorded to the nearest half during the term. The value is
		# never rounded to fit — a 7.3 is a data-entry error to be corrected,
		# not something to silently turn into 7.5.
		if (score * 2) % 1 != 0:
			rejected.append(
				{"student": row.get("student_name") or student, "reason": "step"}
			)
			continue

		existing = frappe.db.get_value(
			"MS Gradebook Entry",
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
			doc = frappe.get_doc("MS Gradebook Entry", existing)
			doc.update(values)
			doc.save()
			updated += 1
		else:
			# New marks start as a draft. A teacher marking a set of papers
			# should not have every intermediate value visible to the class;
			# publishing is a separate, deliberate action.
			doc = frappe.get_doc(
				{"doctype": "MS Gradebook Entry", **values, "ms_is_published": 0}
			)
			doc.insert()
			saved += 1

	frappe.db.commit()

	message_ar = f"تم حفظ {saved + updated} درجة."
	if skipped:
		message_ar += f" تم تجاوز {len(skipped)} درجة تفوق الحد الأقصى."
	step_errors = [r["student"] for r in rejected if r["reason"] == "step"]
	negative = [r["student"] for r in rejected if r["reason"] == "negative"]
	if step_errors:
		message_ar += (
			f" {len(step_errors)} درجة غير مقبولة — يجب أن تكون من مضاعفات ٠.٥."
		)
	if negative:
		message_ar += f" {len(negative)} درجة سالبة غير مقبولة."

	return {
		"success": True,
		"data": {
			"created": saved,
			"updated": updated,
			"skipped": skipped,
			"rejected": rejected,
		},
		"message_en": f"Saved {saved + updated} marks.",
		"message_ar": message_ar,
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_grid(payload: str | dict, persona: str = None):
	"""Save the whole sheet — every column, every student, in one go.

	The column-at-a-time screen made a teacher pick a component, mark thirty
	students, save, and repeat for each assessment. On a spreadsheet the marks
	are entered wherever the eye lands, so the save has to take everything at
	once.

	Each column is delegated to `save_marks`, which already owns the
	validation: nothing is written unless the whole column is valid, so a typo
	in one cell cannot leave half a sheet saved.

	payload = {
	  student_group, course, academic_year, academic_term,
	  columns: [{component_name, component_type, max_score, weight, is_bonus,
	             marks: [{student, score}]}]
	}
	"""
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	if not data:
		return fail(message_en="No data supplied.", message_ar="لم يتم إرسال أي بيانات.")

	columns = data.get("columns") or []
	if not columns:
		return fail(
			message_en="No columns to save.",
			message_ar="لا توجد أعمدة للحفظ.",
		)

	saved = updated = 0
	problems: list[str] = []

	for column in columns:
		if not column.get("component_name") or not (column.get("marks") or []):
			continue
		result = save_marks(
			payload={
				"student_group": data.get("student_group"),
				"course": data.get("course"),
				"academic_year": data.get("academic_year"),
				"academic_term": data.get("academic_term"),
				"component_name": column.get("component_name"),
				"component_type": column.get("component_type"),
				"max_score": column.get("max_score"),
				"weight": column.get("weight"),
				"is_bonus": column.get("is_bonus"),
				"marks": column.get("marks"),
			},
			persona=persona,
		)
		body = result.get("data") if isinstance(result, dict) else None
		if isinstance(result, dict) and result.get("success") is False:
			# Name the column: "a mark is out of range" is unusable when
			# fourteen columns were sent at once.
			problems.append(
				"{0}: {1}".format(
					column.get("component_name"),
					(result.get("message_ar") or result.get("message_en") or "").split("\n")[0],
				)
			)
			continue
		if body:
			saved += cint(body.get("saved"))
			updated += cint(body.get("updated"))

	if problems:
		return fail(
			message_en="Some columns were not saved.",
			message_ar="تعذّر حفظ بعض الأعمدة:\n" + "\n".join(f"• {p}" for p in problems[:8]),
			data={"problems": problems, "saved": saved, "updated": updated},
		)

	return {
		"success": True,
		"data": {"saved": saved, "updated": updated, "columns": len(columns)},
		"message_en": f"Saved {saved + updated} marks.",
		"message_ar": f"تم حفظ {saved + updated} علامة.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def set_aggregation(
	course: str = None,
	component_name: str = None,
	mode: str = "sum",
	n: int = 0,
	program: str = None,
	persona: str = None,
):
	"""Change how a category combines the assessments inside it.

	The choice belongs to the plan, not to one class: a teacher who decides to
	count the best three of four short tests means it for everyone sitting that
	plan. It is stored on the category and every sheet reading that plan picks
	it up, which is why the sheet refetches rather than guessing locally.
	"""
	from match_schools.api.gradeflow import assert_teacher_owns_course

	if not course or not component_name:
		return fail(
			message_en="A course and a category are required.",
			message_ar="يجب تحديد المادة والبند.",
		)
	assert_teacher_owns_course(persona, course)

	if mode not in ("sum", "average", "best_n", "worst_drop"):
		return fail(
			message_en="Unknown aggregation mode.",
			message_ar="طريقة احتساب غير معروفة.",
		)

	n = cint(n)
	if mode in ("best_n", "worst_drop") and n < 1:
		return fail(
			message_en="Give how many assessments to keep or drop.",
			message_ar="حدّد عدد الاختبارات المطلوب احتسابها أو استبعادها.",
		)

	scheme_name = frappe.db.get_value("MS Grade Scheme", {"course": course, "program": program}, "name") \
		or frappe.db.get_value("MS Grade Scheme", {"course": course}, "name")
	if not scheme_name:
		return fail(
			message_en="No assessment plan for this course.",
			message_ar="لا توجد خطة تقييم لهذه المادة.",
		)

	rows = frappe.get_all(
		"MS Grade Scheme Component",
		filters={
			"parent": scheme_name,
			"component_name": component_name,
			"ms_parent_component": ["in", ["", None]],
		},
		pluck="name",
	)
	if not rows:
		return fail(
			message_en="That category is not in this plan.",
			message_ar="هذا البند غير موجود في خطة المادة.",
		)

	for name in rows:
		frappe.db.set_value("MS Grade Scheme Component", name, "ms_aggregation", mode)
		frappe.db.set_value("MS Grade Scheme Component", name, "ms_aggregation_n", n)
	frappe.db.commit()

	label = {
		"sum": "جمع العلامات",
		"average": "متوسط النسب",
		"best_n": f"أفضل {n}",
		"worst_drop": f"استبعاد أدنى {n}",
	}[mode]
	return {
		"success": True,
		"data": {"component": component_name, "mode": mode, "n": n},
		"message_en": f"Aggregation set to {mode}.",
		"message_ar": f"تم ضبط احتساب «{component_name}»: {label}.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def exclude_column(
	student_group: str = None,
	course: str = None,
	component_name: str = None,
	excluded: int = 1,
	academic_term: str = None,
	persona: str = None,
):
	"""Drop an assessment from the total, or put it back.

	The marks are kept either way: a cancelled quiz is still a record of what
	the students did, and a teacher who excludes one by mistake has lost
	nothing.
	"""
	from match_schools.api.gradeflow import assert_teacher_teaches

	if not student_group or not course or not component_name:
		return fail(
			message_en="A class, course and component are required.",
			message_ar="يجب تحديد الشعبة والمادة والمكوّن.",
		)
	assert_teacher_teaches(persona, student_group, course)

	filters = {
		"student_group": student_group,
		"course": course,
		"component_name": component_name,
	}
	if academic_term:
		filters["academic_term"] = academic_term

	names = frappe.get_all("MS Gradebook Entry", filters=filters, pluck="name")
	for name in names:
		frappe.db.set_value("MS Gradebook Entry", name, "ms_excluded", cint(excluded))
	frappe.db.commit()

	return {
		"success": True,
		"data": {"component": component_name, "excluded": bool(cint(excluded)), "rows": len(names)},
		"message_en": f"{component_name} {'excluded' if cint(excluded) else 'included'}.",
		"message_ar": (
			f"تم استبعاد «{component_name}» من الاحتساب."
			if cint(excluded)
			else f"تمت إعادة «{component_name}» إلى الاحتساب."
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def curve_column(
	student_group: str = None,
	course: str = None,
	component_name: str = None,
	points: float = 0,
	percent: float = 0,
	academic_term: str = None,
	persona: str = None,
):
	"""Move a whole column up or down.

	A curve is a decision about the paper, not about a student — the exam was
	harder than intended, so everyone gains two marks. Doing that by hand for
	thirty students invites the one typo nobody notices.

	Marks are clamped to the component's maximum and to zero: a curve must not
	invent a score above the paper's own total, or push a zero negative.
	"""
	from match_schools.api.gradeflow import assert_teacher_teaches

	if not student_group or not course or not component_name:
		return fail(
			message_en="A class, course and component are required.",
			message_ar="يجب تحديد الشعبة والمادة والمكوّن.",
		)
	assert_teacher_teaches(persona, student_group, course)

	points = flt(points)
	percent = flt(percent)
	if not points and not percent:
		return fail(
			message_en="Give an amount to curve by.",
			message_ar="حدّد مقدار التعديل.",
		)

	filters = {
		"student_group": student_group,
		"course": course,
		"component_name": component_name,
	}
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["name", "score", "max_score"],
		limit_page_length=0,
	)
	if not rows:
		return fail(
			message_en="No marks recorded for this component yet.",
			message_ar="لا توجد علامات مرصودة لهذا المكوّن.",
		)

	changed = 0
	for r in rows:
		maximum = flt(r.max_score) or 100
		value = flt(r.score)
		value = value + (value * percent / 100) if percent else value + points
		value = max(0, min(round(value, 2), maximum))
		if abs(value - flt(r.score)) >= 0.001:
			frappe.db.set_value("MS Gradebook Entry", r.name, "score", value)
			changed += 1

	frappe.db.commit()
	direction = "رفع" if (points > 0 or percent > 0) else "خفض"
	return {
		"success": True,
		"data": {"changed": changed, "total": len(rows)},
		"message_en": f"Curved {changed} marks.",
		"message_ar": f"تم {direction} {changed} علامة في «{component_name}».",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_mark(entry: str, persona: str = None):
	frappe.delete_doc("MS Gradebook Entry", entry)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": entry},
		"message_en": "Mark deleted.",
		"message_ar": "تم حذف الدرجة.",
	}


# --- Term grades and the academic record -----------------------------------


def apply_aggregation(
	pairs: list[tuple[float, float]], mode: str = "sum", n: int = 0
) -> tuple[float, float]:
	"""Combine a category's assessments into one (earned, outOf) pair.

	`pairs` is (score, max) for each marked assessment. Unmarked ones are the
	caller's business to leave out — a paper nobody sat is not a zero.

	The modes exist because schools do not all add marks the same way:

	- sum: the plain total, which is what a plan meant before this was
	  configurable, so it stays the default.
	- average: the mean of the percentages, rescaled to the assessments'
	  combined maximum. Use when a 5-mark quiz should count as much as a
	  20-mark test.
	- best_n / worst_drop: rank by percentage, then keep or drop. Ranking by
	  raw score would make a 3/5 beat a 19/20.

	Asking for the best 3 of 2 marked papers returns both rather than nothing:
	mid-term, a teacher has not finished setting the papers yet.
	"""
	pairs = [(flt(a), flt(b)) for a, b in pairs if flt(b)]
	if not pairs:
		return 0.0, 0.0

	if mode == "average":
		total_max = sum(b for _, b in pairs)
		mean = sum(a / b for a, b in pairs) / len(pairs)
		return round(mean * total_max, 4), round(total_max, 4)

	if mode in ("best_n", "worst_drop") and n > 0:
		ranked = sorted(pairs, key=lambda x: x[0] / x[1], reverse=True)
		keep = n if mode == "best_n" else max(len(ranked) - n, 1)
		kept = ranked[: min(keep, len(ranked))] or ranked
		return round(sum(a for a, _ in kept), 4), round(sum(b for _, b in kept), 4)

	return round(sum(a for a, _ in pairs), 4), round(sum(b for _, b in pairs), 4)


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

	# An excluded assessment stays on the student's record but is not part of
	# the sum — a cancelled quiz must neither help nor hurt the total.
	entries = [e for e in entries if not cint(e.get("ms_excluded"))]

	graded = [e for e in entries if not _is_bonus(e)]
	bonus = [e for e in entries if _is_bonus(e)]

	# Assessments that sit under a heading carry no weight of their own — the
	# heading does. Left alone, each such assessment fell into the "no weight"
	# branch below and was treated as if it were worth a full 100, so a plan
	# with four short tests under a 10% heading counted them as 400% of the
	# subject. They are pooled here into their heading instead: the children
	# add up, the heading's weight applies once.
	# The entry itself does not record which heading it belongs to — only the
	# plan knows — so the tree is read once, keyed by assessment name.
	names = {e.get("component_name") for e in graded if e.get("component_name")}
	tree: dict[str, str] = {}
	weights: dict[str, float] = {}
	rules: dict[str, tuple[str, int]] = {}

	# The tree must be read from THIS subject's plan and no other. Assessment
	# names repeat across subjects — every plan has an "امتحان يومي 3" — so a
	# lookup by name alone silently filed a mark under a heading belonging to
	# a different subject's plan, and the student's mark moved by several
	# points with nothing on screen to explain it.
	scheme = None
	course = next((e.get("course") for e in graded if e.get("course")), None)
	if course:
		scheme = frappe.db.get_value("MS Grade Scheme", {"course": course}, "name")
	if not scheme and names:
		# Older callers do not pass the course on the entry. Fall back to the
		# plan that actually contains these assessments, preferring the one
		# covering most of them.
		counts: dict[str, int] = {}
		for row in frappe.get_all(
			"MS Grade Scheme Component",
			filters={"component_name": ["in", list(names)]},
			fields=["parent"],
			limit_page_length=0,
		):
			counts[row["parent"]] = counts.get(row["parent"], 0) + 1
		if counts:
			scheme = max(counts, key=lambda k: counts[k])

	if names and scheme:
		for row in frappe.get_all(
			"MS Grade Scheme Component",
			filters={"parent": scheme},
			fields=[
				"component_name", "ms_parent_component", "weight",
				"ms_aggregation", "ms_aggregation_n",
			],
			limit_page_length=0,
		):
			if row.get("ms_parent_component"):
				if row["component_name"] in names:
					tree[row["component_name"]] = row["ms_parent_component"]
			else:
				weights[row["component_name"]] = flt(row.get("weight"))
				rules[row["component_name"]] = (
					row.get("ms_aggregation") or "sum",
					cint(row.get("ms_aggregation_n")),
				)

	by_parent: dict[str, list] = {}
	standalone = []
	for e in graded:
		parent = tree.get(e.get("component_name"))
		if parent:
			by_parent.setdefault(parent, []).append(
				(flt(e.get("score")), flt(e.get("max_score")))
			)
		else:
			standalone.append(e)

	weighted_sum = 0.0
	covered = 0.0

	for parent, pairs in by_parent.items():
		weight = weights.get(parent, 0.0)
		if not weight:
			continue
		mode, n = rules.get(parent, ("sum", 0))
		earned, out_of = apply_aggregation(pairs, mode, n)
		if not out_of:
			continue
		weighted_sum += (earned / out_of) * weight
		covered += weight

	for e in standalone:
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


def _planned_subject_grade(student: str, course: str, academic_term: str | None) -> dict | None:
	"""One subject's final, worked through its assessment plan.

	Returns None when the subject has no plan for this term, so a school that
	has not built one yet still sees the flat weighted average it saw before.
	"""
	from match_schools.api.assessment_plan import compute_marks

	groups = frappe.get_all(
		"Student Group Student",
		filters={"student": student, "parenttype": "Student Group"},
		pluck="parent",
		limit=10,
	)
	if not groups:
		return None

	for group in groups:
		try:
			res = compute_marks(
				student_group=group,
				course=course,
				academic_term=academic_term,
				student=student,
				persona=ROLE_ADMIN,
			)
		except frappe.PermissionError:
			# Never silently: a permission error here means the caller cannot
			# read the plan behind their own mark, and falling through to the
			# flat average would quietly show a different number than the
			# teacher's — the portal read 78.9 for a subject worth 84.5.
			frappe.log_error(
				title="Subject final fell back to flat average",
				message=(
					f"student={student} course={course} term={academic_term} "
					f"user={frappe.session.user}: plan not readable."
				),
			)
			continue
		except Exception:
			# A malformed plan must not take a family's whole record down.
			continue

		payload = res.get("data") if isinstance(res, dict) and "data" in res else res
		if not payload or not payload.get("students"):
			continue

		row = payload["students"][0]
		if not row.get("quarters"):
			continue

		# Quarters exist but nothing was assessed in them: the plan is an empty
		# shell for this subject. Returning 0 here would print "راسب" for a
		# subject nobody has examined yet, so fall back to the flat average.
		if not any(q.get("categories") for q in row["quarters"]):
			continue

		final = flt(row.get("percent"))
		return {
			"final": round(final, 1),
			"percentage": round(final, 1),
			"bonus": 0.0,
			"covered": 100.0,
			"marks": flt(row.get("marks")),
			"totalMarks": flt(row.get("totalMarks")),
			# Kept so a screen can show the quarters behind the final.
			"quarters": row.get("quarters"),
			**grade_for(final),
		}
	return None


def _class_averages_for(
	student: str, courses: list[str], academic_term: str | None
) -> dict[tuple, float]:
	"""Average percentage per assessment, across the student's own classes.

	Compared within the sections this student belongs to rather than the whole
	school: "above average" only means something against the children sitting
	the same paper. Returns {(course, component): percentage}.
	"""
	if not courses:
		return {}

	groups = frappe.get_all(
		"Student Group Student",
		filters={"student": student, "parenttype": "Student Group"},
		pluck="parent",
		limit=20,
	)
	if not groups:
		return {}

	filters = {
		"course": ["in", courses],
		"student_group": ["in", groups],
	}
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["course", "component_name", "score", "max_score"],
		limit_page_length=0,
	)

	buckets: dict[tuple, list] = {}
	for r in rows:
		maximum = flt(r.max_score)
		if not maximum:
			continue
		buckets.setdefault((r.course, r.component_name), []).append(
			flt(r.score) / maximum * 100
		)

	# A single mark is not an average — showing one would tell a family their
	# child is exactly average when they are the only one scored.
	return {
		key: round(sum(values) / len(values), 1)
		for key, values in buckets.items()
		if len(values) >= 2
	}


def _subject_averages_for(
	student: str, courses: list[str], academic_term: str | None
) -> dict[str, dict]:
	"""Each subject's average twice over: this section, and the whole grade.

	A family asks two different questions about a mark — "how did the class
	do?" and "how did the year group do?" — and a section that happens to be
	strong or weak answers only the first. Both are computed from the same
	published marks the student's own final is built from.

	Returns {course: {"section": pct, "grade": pct, "section_name": str}}.
	"""
	if not courses:
		return {}

	groups = frappe.get_all(
		"Student Group Student",
		filters={"student": student, "parenttype": "Student Group"},
		pluck="parent",
		limit=20,
	)
	if not groups:
		return {}

	# The batch ties sections together: "الصف الأول - أ" and "- ب" share one.
	# Without it "the grade" would silently mean "this section" again.
	batches = {
		g.batch: g.name
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", groups]},
			fields=["name", "batch"],
		)
		if g.batch
	}

	peer_groups = list(groups)
	if batches:
		peer_groups = frappe.get_all(
			"Student Group",
			filters={"batch": ["in", list(batches)]},
			pluck="name",
			limit=200,
		) or list(groups)

	filters = {"course": ["in", courses], "student_group": ["in", peer_groups]}
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["course", "student", "student_group", "score", "max_score"],
		limit_page_length=0,
	)

	# Average per student first, then across students. Averaging raw entries
	# would weight a subject by how many assessments it happens to have.
	per_student: dict[tuple, list] = {}
	for r in rows:
		maximum = flt(r.max_score)
		if not maximum:
			continue
		in_section = r.student_group in groups
		per_student.setdefault((r.course, r.student, in_section), []).append(
			flt(r.score) / maximum * 100
		)

	section_bucket: dict[str, list] = {}
	grade_bucket: dict[str, list] = {}
	for (course, _stu, in_section), marks in per_student.items():
		average = sum(marks) / len(marks)
		grade_bucket.setdefault(course, []).append(average)
		if in_section:
			section_bucket.setdefault(course, []).append(average)

	section_name = ", ".join(sorted(groups)) if groups else ""

	out: dict[str, dict] = {}
	for course in courses:
		section = section_bucket.get(course) or []
		whole = grade_bucket.get(course) or []
		entry: dict = {"section_name": section_name}
		if len(section) >= 2:
			entry["section"] = round(sum(section) / len(section), 1)
		if len(whole) >= 2:
			entry["grade"] = round(sum(whole) / len(whole), 1)
		if len(entry) > 1:
			out[course] = entry
	return out


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
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
		"MS Gradebook Entry",
		filters=filters,
		fields=[
			"name", "course", "component_name", "component_type", "score",
			"max_score", "weight", "is_bonus", "percentage", "remarks",
			"academic_term", "entry_date", "ms_is_published", "ms_release_on",
		],
		order_by="course, entry_date",
	)

	from match_schools.api.gradeflow import courses_of_instructor, term_is_published

	# A teacher sees only the subjects they teach — never a student's marks in
	# someone else's subject.
	own_courses = None
	if persona == ROLE_TEACHER:
		own_courses = courses_of_instructor(resolve_scope(persona).get("instructor"))
		entries = [e for e in entries if e.course in own_courses]

	# A draft mark is the teacher's working copy. Families see a component only
	# once it has been published, so a half-marked quiz never appears as if it
	# were the final result.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		# Two conditions, not one: the mark must be published *and* — when the
		# school set a release date — that date must have arrived. Relying on
		# the nightly job alone would show a mark early if it ran late, or if
		# someone published by hand while a date was still pending.
		today_date = getdate(nowdate())

		def visible(e) -> bool:
			if not cint(e.get("ms_is_published")):
				return False
			release = e.get("ms_release_on")
			return not release or getdate(release) <= today_date

		entries = [e for e in entries if visible(e)]

	by_course: dict[str, list] = {}
	for e in entries:
		by_course.setdefault(e.course, []).append(e)

	# The class average for each assessment, so a family can see whether a mark
	# is above or below what the class managed. One query for the whole record
	# rather than one per component.
	component_averages = _class_averages_for(student, list(by_course), academic_term)
	# The same comparison one level up: how the section and the whole grade did
	# in each subject, next to what this student earned in it.
	subject_averages = _subject_averages_for(student, list(by_course), academic_term)

	subjects = []
	for course, rows in by_course.items():
		# The subject's final comes from the assessment plan and the teacher's
		# own counting rule ("best three of four"), not a flat weighted average
		# of every mark entered. Falling back to the flat average keeps the
		# subjects that have no plan working exactly as before.
		computed = _planned_subject_grade(student, course, academic_term) or (
			_compute_subject_grade(rows)
		)
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
						# What the rest of the class scored on this same
						# assessment, and which side of it this student is on.
						"class_average": component_averages.get((course, r.component_name)),
						**grade_for(flt(r.percentage)),
					}
					for r in rows
				],
				# How the section and the year group did in this same subject.
				"section_average": (subject_averages.get(course) or {}).get("section"),
				"grade_average": (subject_averages.get(course) or {}).get("grade"),
				"section_name": (subject_averages.get(course) or {}).get("section_name"),
				**computed,
			}
		)

	subjects.sort(key=lambda s: s["course"] or "")

	# The Final Term Average is the one figure that is rounded, and it is
	# rounded to a whole number. Subject finals and individual marks keep their
	# decimals: rounding them would compound across components and change a
	# result the teacher actually entered.
	overall_exact = (
		sum(s["final"] for s in subjects) / len(subjects) if subjects else 0.0
	)
	overall = round(overall_exact) if subjects else 0
	# Kept so a screen can show the working, and so a borderline case is
	# auditable rather than looking arbitrary.
	overall_precise = round(overall_exact, 2) if subjects else 0.0

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
		result["overall_precise"] = overall_precise
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def academic_record(student: str, persona: str = None):
	"""The student's whole history: every year and term, with final grades."""
	_assert_can_see(persona, student)

	periods = frappe.db.sql(
		"""
		SELECT DISTINCT academic_year, academic_term
		FROM `tabMS Gradebook Entry`
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def class_term_grades(
	student_group: str,
	course: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Term grades for a whole class — the teacher's overview."""

	# Whole-class marks for one subject: the teacher must take that subject in
	# that class. Without a course this is the class's overall standing, which
	# a teacher of the class may see.
	from match_schools.api.gradeflow import assert_teacher_teaches

	if student_group and course:
		assert_teacher_teaches(persona, student_group, course)
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
			"MS Gradebook Entry",
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def importable_assignments(
	student_group: str, course: str = None, academic_term: str = None, persona: str = None
):
	"""Graded assignments that can be carried into the term marks."""
	from match_schools.api.gradeflow import assert_teacher_teaches

	if course:
		assert_teacher_teaches(persona, student_group, course)

	filters = {"student_group": student_group}
	if course:
		filters["course"] = course

	rows = frappe.get_all(
		"MS Assignment",
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
			from match_schools.api.gradeflow import teacher_may_see_course

			if not teacher_may_see_course(persona, r.course):
				continue
		graded = frappe.db.count(
			"MS Assignment Submission",
			{"assignment": r.name, "status": ["in", ["Graded", "Returned"]]},
		)
		# Whether this assignment has already been carried across.
		imported = frappe.db.exists(
			"MS Gradebook Entry",
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
	from match_schools.api.gradeflow import assert_teacher_owns_course

	doc = frappe.db.get_value(
		"MS Assignment",
		assignment,
		["name", "title", "course", "student_group", "program", "maximum_score"],
		as_dict=True,
	)
	if not doc:
		return fail(message_en="Assignment not found.", message_ar="لم يتم العثور على الواجب.")

	assert_teacher_owns_course(persona, doc.course)

	results = frappe.get_all(
		"MS Assignment Submission",
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
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
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
	from match_schools.api.gradeflow import assert_teacher_teaches

	assert_teacher_teaches(persona, student_group, course)

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
			"MS Assignment",
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
		"MS Assignment Submission",
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


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def publish_component(
	student_group: str = None,
	course: str = None,
	component_name: str = None,
	academic_term: str = None,
	published: int = 1,
	persona: str = None,
):
	"""Show a component's marks to students, or take them back.

	Publishing is per component rather than per student: a teacher finishes
	marking one quiz and releases it, and the class sees that quiz. Marks stay
	editable afterwards — unpublishing is for correcting a mistake, not for
	locking anything.
	"""
	if not student_group or not course or not component_name:
		return fail(
			message_en="A class, course and component are required.",
			message_ar="يجب تحديد الشعبة والمادة والمكوّن.",
		)

	# The same guards save_marks uses: the teacher must own the course, and
	# entry must be open for this class and term.
	from match_schools.api.gradeflow import assert_entry_allowed, assert_teacher_teaches

	assert_teacher_teaches(persona, student_group, course)
	assert_entry_allowed(persona, student_group, course, academic_term)

	filters = {
		"student_group": student_group,
		"course": course,
		"component_name": component_name,
	}
	if academic_term:
		filters["academic_term"] = academic_term

	names = frappe.get_all("MS Gradebook Entry", filters=filters, pluck="name")
	if not names:
		return fail(
			message_en="There are no marks to publish for this component.",
			message_ar="لا توجد درجات لنشرها في هذا المكوّن.",
		)

	flag = 1 if cint(published) else 0
	for name in names:
		frappe.db.set_value(
			"MS Gradebook Entry",
			name,
			{
				"ms_is_published": flag,
				"ms_published_on": frappe.utils.now() if flag else None,
			},
			update_modified=False,
		)
	frappe.db.commit()

	return {
		"published": bool(flag),
		"count": len(names),
		"message_ar": (
			f"تم نشر {len(names)} درجة للطلاب."
			if flag
			else f"تم سحب {len(names)} درجة من الطلاب."
		),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def class_standing(student: str, academic_term: str = None, persona: str = None):
	"""How this student compares with their class, without naming anyone else.

	A family is entitled to know where their child stands; they are not
	entitled to other children's marks. So this returns the student's own
	average, the class average, a rank and the class size — and never a list of
	other students.

	Only marks the family can already see are counted, so a rank cannot be used
	to infer an unpublished result.
	"""
	_assert_can_see(persona, student)
	academic_term = academic_term or get_default_academic_term()

	groups = frappe.get_all(
		"Student Group Student",
		filters={"student": student, "parenttype": "Student Group", "active": 1},
		fields=["parent"],
		limit=5,
	)
	if not groups:
		return {"available": False, "reason": "no_group"}

	group = groups[0].parent
	classmates = frappe.get_all(
		"Student Group Student",
		filters={"parent": group, "parenttype": "Student Group"},
		pluck="student",
	)
	if len(classmates) < 2:
		# A rank out of one tells a family nothing and identifies the class.
		return {"available": False, "reason": "class_too_small"}

	filters = {"student": ["in", classmates]}
	if academic_term:
		filters["academic_term"] = academic_term
	# Families only see published marks, so the comparison is built from the
	# same set — otherwise a rank would leak the existence of hidden results.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		filters["ms_is_published"] = 1

	rows = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["student", "score", "max_score"],
		limit_page_length=0,
	)
	if not rows:
		return {"available": False, "reason": "no_marks"}

	totals: dict[str, list] = {}
	for r in rows:
		if not flt(r.max_score):
			continue
		totals.setdefault(r.student, []).append(flt(r.score) / flt(r.max_score) * 100)

	averages = {s: sum(v) / len(v) for s, v in totals.items() if v}
	if student not in averages or len(averages) < 2:
		return {"available": False, "reason": "no_marks"}

	mine = averages[student]
	ordered = sorted(averages.values(), reverse=True)
	# Standard competition ranking: equal averages share a rank, so two pupils
	# on 90 are both 1st and the next is 3rd.
	rank = sum(1 for v in ordered if v > mine) + 1
	class_average = sum(ordered) / len(ordered)

	return {
		"available": True,
		"studentAverage": round(mine, 1),
		"classAverage": round(class_average, 1),
		"difference": round(mine - class_average, 1),
		"rank": rank,
		"classSize": len(averages),
		"highest": round(ordered[0], 1),
		"lowest": round(ordered[-1], 1),
		# Where the student sits, as a percentile band rather than a precise
		# position — kinder to a child near the bottom, and just as useful.
		"topPercent": round(rank / len(averages) * 100),
		"group": group,
		"academicTerm": academic_term,
	}
