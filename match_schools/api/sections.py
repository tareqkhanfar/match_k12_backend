# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Placing students into sections, and moving them between sections.

A school admits a cohort into a grade, then splits that grade into sections
(شعبة أ، ب، ج). Afterwards students move: one child changes section, a group
of ten is rebalanced, somebody withdraws. This module is the one place those
moves happen.

Two records describe where a student sits and they must never disagree:

  * `Student Group Student` — the section's roster, which attendance, the
    gradebook and the timetable all read.
  * `Program Enrollment.student_batch_name` — the enrolment's batch, which
    billing and reporting read.

Every move here writes both. Updating only the roster is the bug this module
exists to prevent: the child would sit in section ب all year while their
invoices and reports still said أ.
"""

import frappe
from frappe import _
from frappe.utils import cint

from match_schools.api.utils import (
	BACK_OFFICE,
	fail,
	ms_endpoint,
	parse_json_arg,
)


def _group(name: str) -> dict:
	doc = frappe.db.get_value(
		"Student Group",
		name,
		[
			"name", "student_group_name", "program", "batch", "academic_year",
			"academic_term", "max_strength", "disabled", "group_based_on",
		],
		as_dict=True,
	)
	if not doc:
		frappe.throw(_("Section {0} does not exist.").format(name))
	return doc


def _roster(group: str) -> list[dict]:
	rows = frappe.get_all(
		"Student Group Student",
		filters={"parent": group, "parenttype": "Student Group"},
		fields=["name", "student", "student_name", "group_roll_number", "active", "idx"],
		order_by="idx",
		limit_page_length=0,
	)
	return [
		{
			"row": r.name,
			"id": r.student,
			"name": r.student_name,
			"rollNumber": cint(r.group_roll_number) or None,
			"active": bool(r.active),
		}
		for r in rows
	]


def _sync_enrollment_batch(student: str, program: str, academic_year: str, batch: str | None):
	"""Point the student's enrolment at the section's batch.

	Only the enrolment for the same programme and year is touched — a student
	who moves section in this year's grade must not have last year's enrolment
	rewritten. `db_set` is used rather than a full save because Program
	Enrollment is submittable and a submitted document refuses an ordinary
	save; the batch is a reporting field, not part of the accounting entry.
	"""
	if not program or not academic_year:
		return

	enrolments = frappe.get_all(
		"Program Enrollment",
		filters={
			"student": student,
			"program": program,
			"academic_year": academic_year,
			"docstatus": ["<", 2],
		},
		pluck="name",
	)
	for name in enrolments:
		frappe.db.set_value(
			"Program Enrollment", name, "student_batch_name", batch, update_modified=False
		)


def _remove_from_group(student: str, group: str):
	rows = frappe.get_all(
		"Student Group Student",
		filters={"parent": group, "parenttype": "Student Group", "student": student},
		pluck="name",
	)
	for row in rows:
		frappe.db.delete("Student Group Student", row)


def _add_to_group(student: str, group: str) -> bool:
	"""Append a student to a section's roster. False if already there."""
	if frappe.db.exists(
		"Student Group Student",
		{"parent": group, "parenttype": "Student Group", "student": student},
	):
		return False

	doc = frappe.get_doc("Student Group", group)
	next_roll = max(
		[cint(r.group_roll_number) for r in doc.students] + [0]
	) + 1
	doc.append(
		"students",
		{
			"student": student,
			"student_name": frappe.db.get_value("Student", student, "student_name"),
			"group_roll_number": next_roll,
			"active": 1,
		},
	)
	doc.save(ignore_permissions=True)
	return True


def _capacity_left(group: dict) -> int | None:
	"""How many more students fit, or None when no limit is set."""
	limit = cint(group.get("max_strength"))
	if not limit:
		return None
	current = frappe.db.count(
		"Student Group Student", {"parent": group["name"], "parenttype": "Student Group"}
	)
	return limit - current


# --- Reading ---------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def program_sections(program: str = None, academic_year: str = None, persona: str = None):
	"""Every section of one grade, with its roster and spare capacity.

	This is the working view for splitting a grade: the sections side by side,
	plus the students admitted to the grade who are not yet in any of them.
	"""
	if not program:
		return fail(
			message_en="A programme is required.",
			message_ar="يجب اختيار الصف.",
		)

	filters: dict = {"program": program, "disabled": 0}
	if academic_year:
		filters["academic_year"] = academic_year

	groups = frappe.get_all(
		"Student Group",
		filters=filters,
		fields=[
			"name", "student_group_name", "batch", "academic_year",
			"academic_term", "max_strength",
		],
		order_by="batch, name",
		limit_page_length=0,
	)

	sections = []
	placed: set[str] = set()
	for g in groups:
		roster = _roster(g.name)
		placed.update(r["id"] for r in roster)
		limit = cint(g.max_strength) or None
		sections.append(
			{
				"id": g.name,
				"name": g.student_group_name,
				"batch": g.batch,
				"academicYear": g.academic_year,
				"academicTerm": g.academic_term,
				"capacity": limit,
				"count": len(roster),
				"spaceLeft": (limit - len(roster)) if limit else None,
				"students": roster,
			}
		)

	# Admitted to the grade but sitting in no section — the ones the split is
	# actually for.
	enrolled = frappe.get_all(
		"Program Enrollment",
		filters={
			"program": program,
			"docstatus": ["<", 2],
			**({"academic_year": academic_year} if academic_year else {}),
		},
		fields=["student", "student_name", "student_batch_name"],
		limit_page_length=0,
	)
	seen: set[str] = set()
	unassigned = []
	for e in enrolled:
		if e.student in placed or e.student in seen:
			continue
		seen.add(e.student)
		unassigned.append(
			{"id": e.student, "name": e.student_name, "batch": e.student_batch_name}
		)

	return {
		"program": program,
		"academicYear": academic_year,
		"sections": sections,
		"unassigned": unassigned,
		"totals": {
			"sections": len(sections),
			"placed": len(placed),
			"unassigned": len(unassigned),
		},
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def section_options(persona: str = None):
	"""Programmes, years and batch names for the assignment screen."""
	return {
		"programs": frappe.get_all("Program", pluck="name", order_by="name"),
		"academicYears": frappe.get_all(
			"Academic Year", pluck="name", order_by="year_start_date desc"
		),
		"academicTerms": frappe.get_all("Academic Term", pluck="name", order_by="name"),
		"batches": frappe.get_all("Student Batch Name", pluck="name", order_by="name"),
	}


# --- Writing ---------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def assign_students(
	students: str | list = None,
	section: str = None,
	persona: str = None,
):
	"""Place one or more students into a section.

	Used both for the first split of a grade and for adding a late arrival.
	Capacity is enforced for the batch as a whole rather than student by
	student, so a move of ten into a section with three seats is refused
	outright instead of half-completing.
	"""
	ids = parse_json_arg(students, []) or []
	if isinstance(ids, str):
		ids = [ids]
	if not ids or not section:
		return fail(
			message_en="Students and a section are required.",
			message_ar="يجب تحديد الطلاب والشعبة.",
		)

	group = _group(section)
	if cint(group.disabled):
		return fail(
			message_en="That section is disabled.",
			message_ar="هذه الشعبة معطّلة.",
		)

	# Students already in the section are not an error; they simply do not move.
	to_add = [
		s
		for s in ids
		if not frappe.db.exists(
			"Student Group Student",
			{"parent": section, "parenttype": "Student Group", "student": s},
		)
	]
	if not to_add:
		return {"added": 0, "message_ar": "جميع الطلاب المحددين في هذه الشعبة بالفعل."}

	space = _capacity_left(group)
	if space is not None and len(to_add) > space:
		return fail(
			message_en=f"Only {space} places left in this section.",
			message_ar=f"لا يتسع في هذه الشعبة سوى {space} طالب.",
		)

	for student in to_add:
		_add_to_group(student, section)
		_sync_enrollment_batch(
			student, group.program, group.academic_year, group.batch
		)

	frappe.db.commit()
	return {
		"added": len(to_add),
		"section": section,
		"message_ar": f"تمت إضافة {len(to_add)} طالب إلى {group.student_group_name}.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def move_students(
	students: str | list = None,
	from_section: str = None,
	to_section: str = None,
	persona: str = None,
):
	"""Move students from one section to another.

	The roster row is deleted from the old section and created in the new one,
	and the enrolment batch follows. Both sections must belong to the same
	programme: moving a child from grade 1 to grade 8 is a re-enrolment, not a
	section change, and doing it here would leave the enrolment describing a
	programme the student no longer studies.
	"""
	ids = parse_json_arg(students, []) or []
	if isinstance(ids, str):
		ids = [ids]
	if not ids or not from_section or not to_section:
		return fail(
			message_en="Students, source and destination are required.",
			message_ar="يجب تحديد الطلاب والشعبة الحالية والشعبة الجديدة.",
		)
	if from_section == to_section:
		return fail(
			message_en="Source and destination are the same section.",
			message_ar="الشعبة الحالية والجديدة متطابقتان.",
		)

	source = _group(from_section)
	target = _group(to_section)

	if source.program != target.program:
		return fail(
			message_en="Both sections must belong to the same programme.",
			message_ar="يجب أن تكون الشعبتان ضمن نفس الصف.",
		)
	if cint(target.disabled):
		return fail(
			message_en="The destination section is disabled.",
			message_ar="الشعبة الجديدة معطّلة.",
		)

	present = [
		s
		for s in ids
		if frappe.db.exists(
			"Student Group Student",
			{"parent": from_section, "parenttype": "Student Group", "student": s},
		)
	]
	if not present:
		return fail(
			message_en="None of those students are in the source section.",
			message_ar="لا يوجد أي من الطلاب المحددين في الشعبة الحالية.",
		)

	space = _capacity_left(target)
	if space is not None and len(present) > space:
		return fail(
			message_en=f"Only {space} places left in the destination section.",
			message_ar=f"لا يتسع في الشعبة الجديدة سوى {space} طالب.",
		)

	for student in present:
		_remove_from_group(student, from_section)
		_add_to_group(student, to_section)
		_sync_enrollment_batch(
			student, target.program, target.academic_year, target.batch
		)

	frappe.db.commit()
	return {
		"moved": len(present),
		"from": from_section,
		"to": to_section,
		"message_ar": (
			f"تم نقل {len(present)} طالب من {source.student_group_name} "
			f"إلى {target.student_group_name}."
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def swap_students(
	student_a: str = None,
	student_b: str = None,
	persona: str = None,
):
	"""Exchange two students between their sections.

	A swap rather than two moves, because two full sections cannot each accept
	an arrival first — one at a time would be refused for lack of space even
	though the result fits exactly.
	"""
	if not student_a or not student_b:
		return fail(
			message_en="Two students are required.",
			message_ar="يجب تحديد طالبين للتبديل.",
		)
	if student_a == student_b:
		return fail(
			message_en="Choose two different students.",
			message_ar="يجب اختيار طالبين مختلفين.",
		)

	def section_of(student: str) -> str | None:
		rows = frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group"},
			fields=["parent"],
			limit=1,
		)
		return rows[0].parent if rows else None

	group_a = section_of(student_a)
	group_b = section_of(student_b)
	if not group_a or not group_b:
		return fail(
			message_en="Both students must already be in a section.",
			message_ar="يجب أن يكون كلا الطالبين ضمن شعبة.",
		)
	if group_a == group_b:
		return fail(
			message_en="Both students are already in the same section.",
			message_ar="الطالبان في نفس الشعبة بالفعل.",
		)

	a = _group(group_a)
	b = _group(group_b)
	if a.program != b.program:
		return fail(
			message_en="Both sections must belong to the same programme.",
			message_ar="يجب أن تكون الشعبتان ضمن نفس الصف.",
		)

	# Remove both before adding either, so capacity is never briefly exceeded.
	_remove_from_group(student_a, group_a)
	_remove_from_group(student_b, group_b)
	_add_to_group(student_a, group_b)
	_add_to_group(student_b, group_a)
	_sync_enrollment_batch(student_a, b.program, b.academic_year, b.batch)
	_sync_enrollment_batch(student_b, a.program, a.academic_year, a.batch)

	frappe.db.commit()
	return {
		"swapped": True,
		"message_ar": (
			f"تم تبديل الطالبين بين {a.student_group_name} و{b.student_group_name}."
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def withdraw_students(
	students: str | list = None,
	section: str = None,
	clear_batch: int = 1,
	persona: str = None,
):
	"""Take students out of a section without removing them from the school.

	The enrolment stays; only its batch is cleared, so the student remains
	admitted to the grade and simply awaits placement. Pass clear_batch=0 to
	leave the recorded batch alone — useful when withdrawing from a section
	that was created by mistake.
	"""
	ids = parse_json_arg(students, []) or []
	if isinstance(ids, str):
		ids = [ids]
	if not ids or not section:
		return fail(
			message_en="Students and a section are required.",
			message_ar="يجب تحديد الطلاب والشعبة.",
		)

	group = _group(section)
	removed = 0
	for student in ids:
		if not frappe.db.exists(
			"Student Group Student",
			{"parent": section, "parenttype": "Student Group", "student": student},
		):
			continue
		_remove_from_group(student, section)
		removed += 1
		if cint(clear_batch):
			_sync_enrollment_batch(student, group.program, group.academic_year, None)

	frappe.db.commit()
	return {
		"removed": removed,
		"message_ar": f"تم سحب {removed} طالب من {group.student_group_name}.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def distribute_students(
	program: str = None,
	academic_year: str = None,
	sections: str | list = None,
	students: str | list = None,
	strategy: str = "balanced",
	persona: str = None,
):
	"""Spread unplaced students across sections in one action.

	`balanced` fills the emptiest section first, so sections end up level;
	`sequential` fills each section to capacity before starting the next.
	Capacity is always respected, and whatever does not fit is reported back
	rather than silently dropped.
	"""
	target_ids = parse_json_arg(sections, []) or []
	if isinstance(target_ids, str):
		target_ids = [target_ids]
	if not target_ids:
		return fail(
			message_en="At least one section is required.",
			message_ar="يجب اختيار شعبة واحدة على الأقل.",
		)

	ids = parse_json_arg(students, []) or []
	if isinstance(ids, str):
		ids = [ids]
	if not ids:
		# Default to everyone in the grade who has no section yet.
		listing = program_sections(program=program, academic_year=academic_year)
		data = listing.get("data") if isinstance(listing, dict) and "data" in listing else listing
		ids = [s["id"] for s in (data or {}).get("unassigned", [])]

	if not ids:
		return fail(
			message_en="There are no unplaced students.",
			message_ar="لا يوجد طلاب بدون شعبة.",
		)

	groups = [_group(g) for g in target_ids]
	programs = {g.program for g in groups}
	if len(programs) > 1:
		return fail(
			message_en="All sections must belong to the same programme.",
			message_ar="يجب أن تكون جميع الشعب ضمن نفس الصف.",
		)

	state = []
	for g in groups:
		count = frappe.db.count(
			"Student Group Student", {"parent": g.name, "parenttype": "Student Group"}
		)
		limit = cint(g.max_strength) or None
		state.append({"group": g, "count": count, "limit": limit})

	placed = 0
	overflow: list[str] = []
	for student in ids:
		if strategy == "sequential":
			candidates = [s for s in state if s["limit"] is None or s["count"] < s["limit"]]
		else:
			candidates = sorted(
				[s for s in state if s["limit"] is None or s["count"] < s["limit"]],
				key=lambda s: s["count"],
			)
		if not candidates:
			overflow.append(student)
			continue

		chosen = candidates[0]
		g = chosen["group"]
		if _add_to_group(student, g.name):
			_sync_enrollment_batch(student, g.program, g.academic_year, g.batch)
			chosen["count"] += 1
			placed += 1

	frappe.db.commit()
	return {
		"placed": placed,
		"overflow": len(overflow),
		"sections": [
			{"id": s["group"].name, "name": s["group"].student_group_name, "count": s["count"]}
			for s in state
		],
		"message_ar": (
			f"تم توزيع {placed} طالب."
			+ (f" لم يتسع المكان لـ {len(overflow)} طالب." if overflow else "")
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_section(
	name: str = None,
	student_group_name: str = None,
	program: str = None,
	batch: str = None,
	academic_year: str = None,
	academic_term: str = None,
	max_strength: int = 0,
	disabled: int = 0,
	persona: str = None,
):
	"""Create a section, or edit one that exists.

	Capacity cannot be set below the number of students already seated —
	silently accepting it would leave a section permanently over its limit and
	every later move refused for reasons the user cannot see.
	"""
	if name:
		doc = frappe.get_doc("Student Group", name)
	else:
		if not program or not academic_year:
			return fail(
				message_en="A programme and academic year are required.",
				message_ar="يجب تحديد الصف والعام الدراسي.",
			)
		doc = frappe.new_doc("Student Group")
		doc.group_based_on = "Batch"

	if student_group_name:
		doc.student_group_name = student_group_name
	if program:
		doc.program = program
	if batch:
		doc.batch = batch
	if academic_year:
		doc.academic_year = academic_year
	if academic_term:
		doc.academic_term = academic_term
	doc.disabled = cint(disabled)

	limit = cint(max_strength)
	if limit:
		seated = len(doc.students or [])
		if limit < seated:
			return fail(
				message_en=f"This section already holds {seated} students.",
				message_ar=f"الشعبة تضم {seated} طالباً بالفعل، لا يمكن جعل السعة أقل.",
			)
	doc.max_strength = limit

	if not doc.student_group_name:
		doc.student_group_name = f"{doc.program} - {doc.batch}" if doc.batch else doc.program

	doc.save(ignore_permissions=True)

	# A rename of the section changes nothing about who sits in it, but the
	# batch may have changed, and every seated student's enrolment must follow.
	for row in doc.students or []:
		_sync_enrollment_batch(row.student, doc.program, doc.academic_year, doc.batch)

	frappe.db.commit()
	return {
		"id": doc.name,
		"name": doc.student_group_name,
		"message_ar": "تم حفظ الشعبة.",
	}
