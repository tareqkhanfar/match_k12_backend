# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The end-of-term grade workflow.

During the term a teacher only enters marks for the subjects they teach. They
never see a student's term average across other subjects — that total belongs
to the administration, not to any one teacher.

At the end of the term:

  1. Each teacher submits their class/subject marks (Draft -> Submitted).
  2. The administration reviews and either returns them for correction
     (-> Returned) or approves them (-> Approved).
  3. Once every subject for a class is approved, the administration publishes
     the term (-> Published), and only then does a student's overall average
     become visible to students, parents and the wider system.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

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
	resolve_scope,
)

STATUS_AR = {
	"Draft": "قيد الإدخال",
	"Submitted": "مُرحّل للإدارة",
	"Returned": "مُعاد للتعديل",
	"Approved": "معتمد",
	"Published": "منشور",
}

# Once marks leave the teacher's hands they may not be edited again until the
# administration sends them back.
LOCKED_FOR_TEACHER = ("Submitted", "Approved", "Published")


# --- Which subjects a teacher owns ----------------------------------------


def courses_of_instructor(instructor: str | None) -> set[str]:
	"""The subjects this instructor actually teaches.

	A teacher assigned to a section teaches whatever that section is taught,
	so both the timetable and the group roster are consulted.
	"""
	if not instructor:
		return set()

	courses = {
		r.course
		for r in frappe.get_all(
			"Course Schedule",
			filters={"instructor": instructor},
			fields=["course"],
			limit=1000,
		)
		if r.course
	}

	groups = [
		r.parent
		for r in frappe.get_all(
			"Student Group Instructor",
			filters={"instructor": instructor, "parenttype": "Student Group"},
			fields=["parent"],
		)
	]
	if groups:
		courses |= {
			r.course
			for r in frappe.get_all(
				"Student Group", filters={"name": ["in", groups]}, fields=["course"]
			)
			if r.course
		}
		# A group with no course of its own still has a timetable.
		courses |= {
			r.course
			for r in frappe.get_all(
				"Course Schedule",
				filters={"student_group": ["in", groups]},
				fields=["course"],
				limit=1000,
			)
			if r.course
		}

	return courses


def teacher_may_see_course(persona: str, course: str, scope: dict | None = None) -> bool:
	"""Whether this persona may read marks for a given subject."""
	if persona in BACK_OFFICE:
		return True
	if persona != ROLE_TEACHER:
		return True
	scope = scope or resolve_scope(persona)
	return course in courses_of_instructor(scope.get("instructor"))


def teacher_teaches_in_group(persona: str, student_group: str, course: str) -> bool:
	"""Whether this teacher takes THIS subject in THIS class.

	`courses_of_instructor` answers the looser question — which subjects the
	teacher touches anywhere — and treats every subject of a section they are
	listed on as theirs. That is right for "may I see this subject at all" and
	wrong for a mark sheet: a biology teacher listed on 1-أ was able to open
	the maths marks for 1-أ, because maths is a subject of a section they are
	on. The pairing is what the timetable records, so the timetable is what is
	asked.
	"""
	if persona in BACK_OFFICE:
		return True
	if persona != ROLE_TEACHER:
		return True
	instructor = resolve_scope(persona).get("instructor")
	if not instructor:
		return False
	if frappe.db.exists(
		"Course Schedule",
		{
			"instructor": instructor,
			"student_group": student_group,
			"course": course,
			"docstatus": ["<", 2],
		},
	):
		return True
	# A per-subject group names its own course and its own instructors, and may
	# have no timetable yet.
	named = frappe.db.get_value("Student Group", student_group, "course")
	if named and named == course:
		return bool(
			frappe.db.exists(
				"Student Group Instructor",
				{
					"parent": student_group,
					"instructor": instructor,
					"parenttype": "Student Group",
				},
			)
		)
	return False


def assert_teacher_teaches(persona: str, student_group: str, course: str):
	if not teacher_teaches_in_group(persona, student_group, course):
		frappe.throw(
			_("You only have access to the subjects you teach in this class."),
			frappe.PermissionError,
		)


def assert_teacher_owns_course(persona: str, course: str):
	if not teacher_may_see_course(persona, course):
		frappe.throw(
			_("You only have access to the subjects you teach."), frappe.PermissionError
		)


# --- Submission state ------------------------------------------------------


def _submission(student_group: str, course: str, academic_term: str | None):
	name = frappe.db.get_value(
		"MS Term Submission",
		{
			"student_group": student_group,
			"course": course,
			"academic_term": academic_term,
		},
		"name",
	)
	return frappe.get_doc("MS Term Submission", name) if name else None


def submission_status(student_group: str, course: str, academic_term: str | None) -> str:
	doc = _submission(student_group, course, academic_term)
	return doc.status if doc else "Draft"


def assert_entry_allowed(persona: str, student_group: str, course: str, academic_term: str | None):
	"""Marks may only be edited while the submission is open to the teacher."""
	if persona in BACK_OFFICE:
		return
	status = submission_status(student_group, course, academic_term)
	if status in LOCKED_FOR_TEACHER:
		frappe.throw(
			_("These marks have been submitted ({0}) and can no longer be edited.").format(
				STATUS_AR.get(status, status)
			),
			frappe.PermissionError,
		)


def term_is_published(student: str, academic_term: str | None) -> bool:
	"""Has the administration published this student's term results?

	The overall average only becomes visible once it has.
	"""
	if not academic_term:
		return False
	groups = [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": student, "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	]
	if not groups:
		return False
	return bool(
		frappe.db.exists(
			"MS Term Submission",
			{
				"student_group": ["in", groups],
				"academic_term": academic_term,
				"status": "Published",
			},
		)
	)


# --- Endpoints -------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def my_submissions(academic_term: str = None, persona: str = None):
	"""What each of my classes/subjects still owes, and where it stands."""
	scope = resolve_scope(persona)
	academic_term = academic_term or get_default_academic_term()

	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		if not instructor:
			return {"rows": [], "academic_term": academic_term}
		pairs = _teacher_class_courses(instructor)
	else:
		pairs = _all_class_courses(academic_term)

	rows = []
	for group, course in pairs:
		doc = _submission(group, course, academic_term)
		roster = frappe.db.count(
			"Student Group Student",
			{"parent": group, "parenttype": "Student Group", "active": 1},
		)
		entered = len(
			{
				e.student
				for e in frappe.get_all(
					"MS Gradebook Entry",
					filters={
						"student_group": group,
						"course": course,
						"academic_term": academic_term,
					},
					fields=["student"],
				)
			}
		)
		rows.append(
			{
				"id": doc.name if doc else None,
				"student_group": group,
				"course": course,
				"status": doc.status if doc else "Draft",
				"status_label": STATUS_AR.get(doc.status if doc else "Draft"),
				"students": roster,
				"entered": entered,
				"complete": roster > 0 and entered >= roster,
				"submitted_on": str(doc.submitted_on or "") if doc else "",
				"review_notes": doc.review_notes if doc else None,
				"instructor": doc.instructor if doc else None,
			}
		)

	rows.sort(key=lambda r: (r["status"] != "Returned", r["student_group"], r["course"]))
	return {"rows": rows, "academic_term": academic_term}


def _teacher_class_courses(instructor: str) -> list[tuple[str, str]]:
	"""Every (class, subject) this teacher is responsible for."""
	pairs = {
		(r.student_group, r.course)
		for r in frappe.get_all(
			"Course Schedule",
			filters={"instructor": instructor},
			fields=["student_group", "course"],
			limit=1000,
		)
		if r.student_group and r.course
	}

	for r in frappe.get_all(
		"Student Group Instructor",
		filters={"instructor": instructor, "parenttype": "Student Group"},
		fields=["parent"],
	):
		course = frappe.db.get_value("Student Group", r.parent, "course")
		if course:
			pairs.add((r.parent, course))
		else:
			pairs |= {
				(r.parent, cs.course)
				for cs in frappe.get_all(
					"Course Schedule",
					filters={"student_group": r.parent},
					fields=["course"],
					limit=200,
				)
				if cs.course
			}
	return sorted(pairs)


def _all_class_courses(academic_term: str | None) -> list[tuple[str, str]]:
	"""Every (class, subject) in the school — the administration's view."""
	filters = {"disabled": 0}
	if academic_term:
		filters["academic_term"] = academic_term
	groups = frappe.get_all("Student Group", filters=filters, pluck="name", limit=300)
	if not groups:
		return []

	pairs = {
		(r.student_group, r.course)
		for r in frappe.get_all(
			"Course Schedule",
			filters={"student_group": ["in", groups]},
			fields=["student_group", "course"],
			limit=2000,
		)
		if r.student_group and r.course
	}
	# Include anything already submitted, even if its timetable has since gone.
	for r in frappe.get_all(
		"MS Term Submission",
		filters={"academic_term": academic_term} if academic_term else {},
		fields=["student_group", "course"],
		limit=1000,
	):
		pairs.add((r.student_group, r.course))
	return sorted(pairs)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def submit_term(
	student_group: str,
	course: str,
	academic_term: str = None,
	notes: str = None,
	persona: str = None,
):
	"""A teacher hands their marks to the administration."""
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		assert_teacher_owns_course(persona, course)

	academic_term = academic_term or get_default_academic_term()
	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "parenttype": "Student Group", "active": 1},
		pluck="student",
	)
	if not roster:
		return fail(
			message_en="This class has no active students.",
			message_ar="لا يوجد طلاب في هذه الشعبة.",
		)

	graded = {
		e.student
		for e in frappe.get_all(
			"MS Gradebook Entry",
			filters={
				"student_group": student_group,
				"course": course,
				"academic_term": academic_term,
			},
			fields=["student"],
		)
	}
	missing = [s for s in roster if s not in graded]
	if missing:
		names = frappe.get_all(
			"Student", filters={"name": ["in", missing[:5]]}, pluck="student_name"
		)
		return fail(
			message_en=f"{len(missing)} student(s) have no marks yet.",
			message_ar=f"{len(missing)} طالب/طلاب بدون علامات: {'، '.join(names)}",
		)

	doc = _submission(student_group, course, academic_term)
	if doc and doc.status in ("Submitted", "Approved", "Published"):
		return fail(
			message_en="These marks have already been submitted.",
			message_ar="تم ترحيل هذه العلامات بالفعل.",
		)

	if not doc:
		doc = frappe.new_doc("MS Term Submission")
		doc.student_group = student_group
		doc.course = course
		doc.academic_term = academic_term
		doc.academic_year = frappe.db.get_value(
			"Student Group", student_group, "academic_year"
		) or get_default_academic_year()
		doc.instructor = scope.get("instructor")

	# What changed since the administration reopened this course, captured at
	# the moment of resubmission. Without it the reviewer would have to take on
	# trust that the teacher edited only the mark under appeal — the whole point
	# of reopening one subject is that the rest stays untouched.
	changed_summary = None
	if doc.get("ms_reopened_on"):
		from match_schools.api.grade_appeals import change_log

		log = change_log(
			student_group=student_group,
			course=course,
			academic_term=academic_term,
			persona=persona,
		)
		data = log.get("data") if isinstance(log, dict) and "data" in log else log
		changed_summary = data or {}

	doc.status = "Submitted"
	doc.students_count = len(roster)
	doc.submitted_by = frappe.session.user
	doc.submitted_on = now_datetime()
	doc.teacher_notes = notes
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	result = {"id": doc.name, "status": doc.status, "students": len(roster)}
	if changed_summary:
		result["changed"] = changed_summary.get("total", 0)
		result["changedStudents"] = changed_summary.get("changedStudents", [])

	return {
		"success": True,
		"data": result,
		"message_en": "Marks submitted to the administration.",
		"message_ar": (
			"تم ترحيل العلامات إلى الإدارة."
			+ (
				f" تم تعديل {changed_summary.get('total', 0)} علامة لـ "
				f"{len(changed_summary.get('changedStudents', []))} طالب."
				if changed_summary and changed_summary.get("total")
				else ""
			)
		),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def review_term(submission: str, action: str, notes: str = None, persona: str = None):
	"""The administration approves a submission or returns it for correction."""
	if action not in ("approve", "return"):
		return fail(message_en="Unknown action.", message_ar="إجراء غير معروف.")

	doc = frappe.get_doc("MS Term Submission", submission)
	if doc.status == "Published":
		return fail(
			message_en="Published marks cannot be reviewed again.",
			message_ar="لا يمكن مراجعة علامات منشورة.",
		)

	doc.status = "Approved" if action == "approve" else "Returned"
	doc.reviewed_by = frappe.session.user
	doc.reviewed_on = now_datetime()
	doc.review_notes = notes
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "status": doc.status},
		"message_en": f"Submission {doc.status.lower()}.",
		"message_ar": "تم اعتماد العلامات." if action == "approve" else "تمت إعادة العلامات للمعلم.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def publish_term(
	student_group: str, academic_term: str = None, force: int = 0, persona: str = None
):
	"""Publish a class's term results, making the overall average visible.

	Every subject must be approved first, unless the administration overrides.
	"""
	academic_term = academic_term or get_default_academic_term()
	submissions = frappe.get_all(
		"MS Term Submission",
		filters={"student_group": student_group, "academic_term": academic_term},
		fields=["name", "course", "status"],
	)
	if not submissions:
		return fail(
			message_en="Nothing has been submitted for this class yet.",
			message_ar="لم يتم ترحيل أي علامات لهذه الشعبة بعد.",
		)

	pending = [s for s in submissions if s.status not in ("Approved", "Published")]
	if pending and not cint(force):
		return fail(
			message_en=f"{len(pending)} subject(s) are not approved yet.",
			message_ar=f"{len(pending)} مادة/مواد لم تُعتمد بعد: "
			+ "، ".join(s.course for s in pending[:5]),
		)

	published = 0
	for s in submissions:
		if s.status == "Published":
			continue
		doc = frappe.get_doc("MS Term Submission", s.name)
		doc.status = "Published"
		doc.published_by = frappe.session.user
		doc.published_on = now_datetime()
		doc.save(ignore_permissions=True)
		published += 1

	frappe.db.commit()
	return {
		"success": True,
		"data": {"published": published, "student_group": student_group},
		"message_en": f"Published {published} subject(s).",
		"message_ar": f"تم نشر نتائج {published} مادة/مواد لهذه الشعبة.",
	}


def _changes_since_reopen(
	student_group: str, course: str, academic_term: str | None, reopened_on
) -> int:
	"""How many marks moved since this course was reopened.

	Counted here rather than in the change log so the overview can show a badge
	without loading every version row for every subject on the page.
	"""
	if not reopened_on:
		return 0
	entries = frappe.get_all(
		"MS Gradebook Entry",
		filters={
			"student_group": student_group,
			"course": course,
			**({"academic_term": academic_term} if academic_term else {}),
		},
		pluck="name",
	)
	if not entries:
		return 0
	rows = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabVersion`
		 WHERE ref_doctype = 'MS Gradebook Entry'
		   AND docname IN %(names)s
		   AND creation > %(since)s
		   AND data LIKE '%%"score"%%'
		""",
		{"names": tuple(entries), "since": reopened_on},
	)
	return cint(rows[0][0]) if rows else 0


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def term_overview(academic_term: str = None, persona: str = None):
	"""Where every class stands — the administration's publishing console."""
	academic_term = academic_term or get_default_academic_term()
	filters = {"disabled": 0}
	if academic_term:
		# A section that names no term belongs to whichever term is current —
		# filtering on an exact match hid every class on a school that leaves
		# the field blank, so the console came up empty and nothing could be
		# published or withdrawn.
		filters["academic_term"] = ["in", [academic_term, ""]]
	groups = frappe.get_all(
		"Student Group", filters=filters, fields=["name", "student_group_name"], limit=300
	)

	rows = []
	for g in groups:
		subs = frappe.get_all(
			"MS Term Submission",
			filters={"student_group": g.name, "academic_term": academic_term},
			fields=[
				"name", "course", "status", "submitted_on", "instructor",
				"ms_reopened_on", "ms_reopened_by", "ms_reopen_reason",
			],
		)
		expected = len(
			{
				r.course
				for r in frappe.get_all(
					"Course Schedule",
					filters={"student_group": g.name},
					fields=["course"],
					limit=200,
				)
				if r.course
			}
		)
		by_status = {}
		for s in subs:
			by_status[s.status] = by_status.get(s.status, 0) + 1

		rows.append(
			{
				"student_group": g.name,
				"name": g.student_group_name or g.name,
				"expected": expected,
				"submitted": len(subs),
				"approved": by_status.get("Approved", 0),
				"published": by_status.get("Published", 0),
				"returned": by_status.get("Returned", 0),
				"ready": len(subs) > 0
				and all(s.status in ("Approved", "Published") for s in subs),
				"is_published": len(subs) > 0 and all(s.status == "Published" for s in subs),
				"subjects": [
					{
						"id": s.name,
						"course": s.course,
						"status": s.status,
						"status_label": STATUS_AR.get(s.status, s.status),
						"instructor": s.instructor,
						"submitted_on": str(s.submitted_on or ""),
						# A subject that was reopened for an appeal carries its
						# reason and how many marks moved since, so the reviewer
						# can check the teacher touched only what was disputed.
						"reopened_on": str(s.ms_reopened_on or ""),
						"reopened_by": s.ms_reopened_by,
						"reopen_reason": s.ms_reopen_reason,
						"changed_count": _changes_since_reopen(
							g.name, s.course, academic_term, s.ms_reopened_on
						),
					}
					for s in subs
				],
			}
		)

	rows.sort(key=lambda r: (r["is_published"], r["name"]))
	return {"rows": rows, "academic_term": academic_term}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def unpublish_term(
	student_group: str, academic_term: str = None, reason: str = None, persona: str = None
):
	"""Reopen a published term so a genuine error can be corrected.

	Publishing is meant to be final, so this is deliberately admin-only and
	records why it happened. Results disappear from students and parents again
	until the term is republished.
	"""
	academic_term = academic_term or get_default_academic_term()
	rows = frappe.get_all(
		"MS Term Submission",
		filters={
			"student_group": student_group,
			"academic_term": academic_term,
			"status": "Published",
		},
		pluck="name",
	)
	if not rows:
		return fail(
			message_en="Nothing is published for this class.",
			message_ar="لا توجد نتائج منشورة لهذه الشعبة.",
		)

	for name in rows:
		doc = frappe.get_doc("MS Term Submission", name)
		doc.status = "Approved"
		doc.published_by = None
		doc.published_on = None
		doc.review_notes = reason or _("Reopened by the administration.")
		doc.save(ignore_permissions=True)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"reopened": len(rows), "student_group": student_group},
		"message_en": f"Reopened {len(rows)} subject(s).",
		"message_ar": f"تم إعادة فتح {len(rows)} مادة/مواد — النتائج لم تعد ظاهرة للطلاب.",
	}
