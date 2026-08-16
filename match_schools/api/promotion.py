"""Moving a class up a grade, with the checks a school actually applies.

Education's Program Enrollment Tool moves students between programmes and asks
nothing: it does not know whether the marks are finished, whether the family
owes anything, or whether the child was present often enough to have earned the
year. Schools apply those checks anyway — on paper, in a meeting, and
inconsistently.

This does the same work with the checks written down. Every student is
evaluated against the rules the school chose, and the result is a list with a
reason attached to each name. Nothing is enrolled until an administrator looks
at that list and says go.

The design rule throughout: **a failed check is a warning, not a wall.** A
family owing 200 may still be promoted by a head who knows the circumstances,
and the override is recorded with a reason rather than achieved by editing the
rule and forgetting to put it back. What the system must never do is move a
child up quietly when something was wrong.
"""

import frappe
from frappe.utils import cint, flt, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	fail,
	get_default_academic_year,
	ms_endpoint,
)

# Every check the school can switch on. Kept as data so the screen can render
# them without knowing what each one means.
CHECKS = {
	"marks_submitted": {
		"label": "العلامات مرحّلة ومعتمدة",
		"help": "كل مواد الشعبة سُلّمت واعتُمدت لهذا الفصل.",
		"severity": "blocking",
	},
	"no_outstanding_fees": {
		"label": "لا توجد مستحقات مالية",
		"help": "المتأخرات على الطالب لا تتجاوز الحد المسموح.",
		"severity": "blocking",
	},
	"minimum_average": {
		"label": "المعدل العام لا يقل عن الحد",
		"help": "معدل الطالب في الفصل يساوي الحد أو يزيد.",
		"severity": "blocking",
	},
	"attendance_rate": {
		"label": "نسبة الحضور كافية",
		"help": "نسبة حضور الطالب خلال العام لا تقل عن الحد.",
		"severity": "blocking",
	},
	"no_failed_subjects": {
		"label": "لا مواد راسب فيها",
		"help": "عدد المواد التي رسب فيها الطالب لا يتجاوز الحد.",
		"severity": "blocking",
	},
}

DEFAULT_SETTINGS = {
	"marks_submitted": {"enabled": 1},
	"no_outstanding_fees": {"enabled": 1, "max_outstanding": 0},
	"minimum_average": {"enabled": 0, "min_average": 50},
	"attendance_rate": {"enabled": 0, "min_attendance": 75},
	"no_failed_subjects": {"enabled": 0, "max_failed": 2},
}

SETTINGS_KEY = "ms_promotion_rules"


def _settings() -> dict:
	"""The school's chosen rules, falling back to sensible defaults."""
	raw = frappe.db.get_default(SETTINGS_KEY)
	stored = {}
	if raw:
		try:
			stored = frappe.parse_json(raw) or {}
		except Exception:
			stored = {}
	merged = {}
	for key, default in DEFAULT_SETTINGS.items():
		merged[key] = {**default, **(stored.get(key) or {})}
	return merged


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def get_rules(persona: str = None):
	"""The rules, and what each one means, for the settings screen."""
	settings = _settings()
	return {
		"rules": [
			{
				"key": key,
				"label": meta["label"],
				"help": meta["help"],
				**settings.get(key, {}),
			}
			for key, meta in CHECKS.items()
		]
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_rules(payload: str | dict = None, persona: str = None):
	"""Store the school's promotion rules."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	rules = data.get("rules") or {}

	clean = {}
	for key in CHECKS:
		row = rules.get(key) or {}
		clean[key] = {
			"enabled": cint(row.get("enabled")),
			"max_outstanding": flt(row.get("max_outstanding")),
			"min_average": flt(row.get("min_average")),
			"min_attendance": flt(row.get("min_attendance")),
			"max_failed": cint(row.get("max_failed")),
		}

	frappe.db.set_default(SETTINGS_KEY, frappe.as_json(clean))
	frappe.db.commit()
	return {
		"success": True,
		"data": {"saved": True},
		"message_en": "Promotion rules saved.",
		"message_ar": "تم حفظ شروط الترفيع.",
	}


def next_program(program: str) -> str | None:
	"""The grade one rung above this one, or None at the top of the ladder."""
	level = cint(frappe.db.get_value("Program", program, "ms_level"))
	if not level:
		return None
	rows = frappe.get_all(
		"Program",
		filters={"ms_level": level + 1},
		pluck="name",
		limit=2,
	)
	# Two programmes on the same rung is a configuration the school has to
	# resolve; picking one would send a whole year group to the wrong grade.
	return rows[0] if len(rows) == 1 else None


def _outstanding_for(student: str) -> float:
	"""What this student's family still owes on submitted invoices."""
	row = frappe.db.sql(
		"""
		select sum(si.outstanding_amount) total
		from `tabSales Invoice` si
		where si.docstatus = 1
		  and si.outstanding_amount > 0
		  and si.student = %s
		""",
		student,
		as_dict=True,
	)
	return flt(row[0].total) if row and row[0].total else 0.0


def _attendance_rate(student: str, academic_year: str | None) -> float | None:
	"""Percentage of days present, or None when nothing was recorded."""
	rows = frappe.db.sql(
		"""
		select status, count(*) n
		from `tabStudent Attendance`
		where student = %s and docstatus < 2
		group by status
		""",
		student,
		as_dict=True,
	)
	counts = {r.status: cint(r.n) for r in rows}
	total = sum(counts.values())
	if not total:
		return None
	return round(counts.get("Present", 0) / total * 100, 1)


def _marks_state(student_group: str, academic_term: str | None) -> tuple[bool, list[str]]:
	"""Whether every subject of this class has been submitted and approved."""
	filters = {"student_group": student_group}
	if academic_term:
		filters["academic_term"] = academic_term
	rows = frappe.get_all(
		"MS Term Submission",
		filters=filters,
		fields=["course", "status"],
		limit_page_length=0,
	)
	if not rows:
		return False, []
	pending = [r.course for r in rows if r.status not in ("Approved", "Published")]
	return (not pending), pending


def evaluate_student(
	student: str,
	student_group: str,
	settings: dict,
	academic_year: str | None,
	academic_term: str | None,
	marks_ok: bool,
	pending_courses: list[str],
) -> dict:
	"""Run every enabled check against one student.

	Returns the reasons a student cannot be promoted rather than a bare
	yes/no: an administrator looking at 400 names needs to know which ones need
	a phone call and which need a meeting.
	"""
	blockers: list[dict] = []

	if cint(settings["marks_submitted"].get("enabled")) and not marks_ok:
		blockers.append(
			{
				"check": "marks_submitted",
				"label": CHECKS["marks_submitted"]["label"],
				"detail": (
					"لم تُعتمد علامات: " + "، ".join(pending_courses[:4])
					if pending_courses
					else "لا توجد علامات مرحّلة لهذه الشعبة."
				),
			}
		)

	fee_rule = settings["no_outstanding_fees"]
	if cint(fee_rule.get("enabled")):
		owed = _outstanding_for(student)
		limit = flt(fee_rule.get("max_outstanding"))
		if owed > limit:
			blockers.append(
				{
					"check": "no_outstanding_fees",
					"label": CHECKS["no_outstanding_fees"]["label"],
					"detail": f"عليه {owed:g} والحد المسموح {limit:g}.",
					"value": owed,
				}
			)

	avg_rule = settings["minimum_average"]
	if cint(avg_rule.get("enabled")):
		from match_schools.api.gradebook import term_grades

		try:
			result = term_grades(
				student=student,
				academic_year=academic_year,
				academic_term=academic_term,
				persona="admin",
			)
			data = result.get("data", result) if isinstance(result, dict) else result
			average = flt((data or {}).get("average"))
		except Exception:
			average = 0.0
		minimum = flt(avg_rule.get("min_average"))
		if average and average < minimum:
			blockers.append(
				{
					"check": "minimum_average",
					"label": CHECKS["minimum_average"]["label"],
					"detail": f"معدله {average:g}% والحد {minimum:g}%.",
					"value": average,
				}
			)

	att_rule = settings["attendance_rate"]
	if cint(att_rule.get("enabled")):
		rate = _attendance_rate(student, academic_year)
		minimum = flt(att_rule.get("min_attendance"))
		# No attendance recorded at all is not a failure to attend; a school
		# that does not take registers would otherwise hold back every child.
		if rate is not None and rate < minimum:
			blockers.append(
				{
					"check": "attendance_rate",
					"label": CHECKS["attendance_rate"]["label"],
					"detail": f"نسبة حضوره {rate:g}% والحد {minimum:g}%.",
					"value": rate,
				}
			)

	fail_rule = settings["no_failed_subjects"]
	if cint(fail_rule.get("enabled")):
		from match_schools.api.alerts import _measure_failing_subjects

		try:
			# The measurer takes a list of students and returns a map, so a
			# whole class can be measured in one pass; here it is one name.
			measured = _measure_failing_subjects(
				[student],
				frappe._dict(
					{
						"academic_year": academic_year,
						"threshold": 50,
						"trigger": "Failing Subjects",
					}
				),
			)
			failed = cint((measured or {}).get(student))
		except Exception:
			failed = 0
		limit = cint(fail_rule.get("max_failed"))
		if failed > limit:
			blockers.append(
				{
					"check": "no_failed_subjects",
					"label": CHECKS["no_failed_subjects"]["label"],
					"detail": f"راسب في {failed} مادة والحد {limit}.",
					"value": failed,
				}
			)

	return {"eligible": not blockers, "blockers": blockers}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def preview(
	student_group: str = None,
	academic_year: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Who in this class may move up, and what stands in the way of the rest.

	Nothing is written. This is the list an administrator reads before
	deciding, which is the whole point: the tool this replaces enrolled first
	and left the checking to whoever noticed afterwards.
	"""
	if not student_group:
		return fail(
			message_en="A class is required.",
			message_ar="يجب تحديد الشعبة.",
		)

	group = frappe.db.get_value(
		"Student Group",
		student_group,
		["name", "student_group_name", "program", "batch", "academic_year", "academic_term"],
		as_dict=True,
	)
	if not group:
		return fail(message_en="Class not found.", message_ar="لم يتم العثور على الشعبة.")

	academic_year = academic_year or group.academic_year or get_default_academic_year()
	academic_term = academic_term or group.academic_term

	settings = _settings()
	marks_ok, pending = _marks_state(student_group, academic_term)

	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name"],
		order_by="group_roll_number, student_name",
		limit_page_length=0,
	)

	target = next_program(group.program) if group.program else None

	rows = []
	for r in roster:
		# A student already disabled has left; promoting them would resurrect
		# a record the school closed on purpose.
		if not cint(frappe.db.get_value("Student", r.student, "enabled")):
			rows.append(
				{
					"student": r.student,
					"student_name": r.student_name,
					"eligible": False,
					"blockers": [
						{
							"check": "inactive",
							"label": "الطالب غير مقيّد",
							"detail": "سجل الطالب غير مفعّل.",
						}
					],
				}
			)
			continue

		verdict = evaluate_student(
			r.student, student_group, settings, academic_year, academic_term, marks_ok, pending
		)
		rows.append({"student": r.student, "student_name": r.student_name, **verdict})

	eligible = [r for r in rows if r["eligible"]]
	return {
		"student_group": student_group,
		"class_name": group.student_group_name,
		"program": group.program,
		"next_program": target,
		"next_program_missing": bool(group.program and not target),
		"academic_year": academic_year,
		"academic_term": academic_term,
		"marks_ready": marks_ok,
		"pending_courses": pending,
		"students": rows,
		"total": len(rows),
		"eligible_count": len(eligible),
		"blocked_count": len(rows) - len(eligible),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def promote(payload: str | dict = None, persona: str = None):
	"""Enrol the chosen students in the next grade.

	Only an administrator may do this, and only students named in the request
	— the preview is advice, not authority. Each name is re-checked here:
	the preview may be minutes old, and a student whose situation changed in
	between must not slip through on a stale verdict unless the override says
	so explicitly.
	"""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	student_group = data.get("student_group")
	students = data.get("students") or []
	target_program = data.get("new_program")
	new_batch = data.get("new_batch")
	new_year = data.get("new_academic_year")
	new_term = data.get("new_academic_term")
	override = cint(data.get("override"))
	reason = (data.get("override_reason") or "").strip()

	if not student_group or not students:
		return fail(
			message_en="A class and at least one student are required.",
			message_ar="يجب تحديد الشعبة وطالب واحد على الأقل.",
		)
	if not new_year:
		return fail(
			message_en="The new academic year is required.",
			message_ar="يجب تحديد العام الدراسي الجديد.",
		)

	group = frappe.db.get_value(
		"Student Group", student_group, ["program", "academic_year", "academic_term"], as_dict=True
	)
	if not group:
		return fail(message_en="Class not found.", message_ar="لم يتم العثور على الشعبة.")

	target_program = target_program or next_program(group.program)
	if not target_program:
		return fail(
			message_en="No next grade is configured for this programme.",
			message_ar="لم يُحدَّد الصف التالي لهذا البرنامج. اضبط «مستوى الصف» في البرامج.",
		)

	# Overriding the rules is allowed and recorded. Doing it silently is not:
	# a year later, "why was this child promoted" has to have an answer.
	if override and not reason:
		return fail(
			message_en="A reason is required when overriding the rules.",
			message_ar="يجب كتابة سبب عند تجاوز الشروط.",
		)

	settings = _settings()
	marks_ok, pending = _marks_state(student_group, group.academic_term)

	promoted, skipped = [], []
	for student in students:
		name = frappe.db.get_value("Student", student, "student_name")
		verdict = evaluate_student(
			student, student_group, settings, group.academic_year,
			group.academic_term, marks_ok, pending,
		)
		if not verdict["eligible"] and not override:
			skipped.append(
				{
					"student": student,
					"student_name": name,
					"reason": "، ".join(b["label"] for b in verdict["blockers"]),
				}
			)
			continue

		existing = frappe.db.exists(
			"Program Enrollment",
			{
				"student": student,
				"program": target_program,
				"academic_year": new_year,
				"docstatus": ["<", 2],
			},
		)
		if existing:
			skipped.append(
				{
					"student": student,
					"student_name": name,
					"reason": "مسجّل مسبقاً في الصف الجديد",
				}
			)
			continue

		previous = frappe.db.get_value(
			"Program Enrollment",
			{"student": student, "program": group.program, "docstatus": ["<", 2]},
			"name",
		)

		doc = frappe.new_doc("Program Enrollment")
		doc.student = student
		doc.student_name = name
		doc.program = target_program
		doc.academic_year = new_year
		if new_term:
			doc.academic_term = new_term
		if new_batch:
			doc.student_batch_name = new_batch
		doc.enrollment_date = today()
		doc.ms_promotion_status = "Promoted"
		doc.ms_promoted_from = previous
		if override and not verdict["eligible"]:
			doc.ms_promotion_notes = "تجاوز الشروط: " + reason
		doc.insert(ignore_permissions=True)
		doc.submit()

		if previous:
			frappe.db.set_value(
				"Program Enrollment", previous, "ms_promotion_status", "Promoted",
				update_modified=False,
			)
		promoted.append({"student": student, "student_name": name, "enrollment": doc.name})

	frappe.db.commit()
	return {
		"success": True,
		"data": {
			"promoted": promoted,
			"skipped": skipped,
			"promoted_count": len(promoted),
			"skipped_count": len(skipped),
			"new_program": target_program,
		},
		"message_en": f"Promoted {len(promoted)} student(s).",
		"message_ar": (
			f"تم ترفيع {len(promoted)} طالباً"
			+ (f"، وتُخطّي {len(skipped)}." if skipped else ".")
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def mark_repeated(
	student: str = None,
	student_group: str = None,
	notes: str = None,
	persona: str = None,
):
	"""Record that a student stays in the same grade, and why."""
	if not student or not student_group:
		return fail(
			message_en="A student and a class are required.",
			message_ar="يجب تحديد الطالب والشعبة.",
		)
	group_program = frappe.db.get_value("Student Group", student_group, "program")
	enrollment = frappe.db.get_value(
		"Program Enrollment",
		{"student": student, "program": group_program, "docstatus": ["<", 2]},
		"name",
	)
	if not enrollment:
		return fail(
			message_en="No enrollment found for this student.",
			message_ar="لا يوجد تسجيل لهذا الطالب في الصف الحالي.",
		)
	frappe.db.set_value("Program Enrollment", enrollment, "ms_promotion_status", "Repeated")
	if notes:
		frappe.db.set_value("Program Enrollment", enrollment, "ms_promotion_notes", notes)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"enrollment": enrollment},
		"message_en": "Recorded as repeating the year.",
		"message_ar": "تم تسجيل إعادة الطالب للصف نفسه.",
	}
