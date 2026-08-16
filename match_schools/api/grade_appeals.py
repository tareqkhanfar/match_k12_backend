# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Reopening published marks, and the record of what changed afterwards.

A student appeals a mark after results are out and turns out to be right. The
administration reopens that one course for that one section — not the whole
term, which would put every other subject back in the teacher's hands — the
teacher corrects the mark, and the change is visible afterwards: old value,
new value, who changed it, when.

Nothing here records the change itself. Frappe already writes every edit to
`MS Gradebook Entry` into the Version table, so this module reads that rather
than keeping a second history that could disagree with the first.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now, nowdate

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	ms_endpoint,
	resolve_scope,
)


def _submission(student_group: str, course: str, academic_term: str | None):
	filters = {"student_group": student_group, "course": course}
	if academic_term:
		filters["academic_term"] = academic_term
	name = frappe.db.get_value("MS Term Submission", filters, "name")
	return frappe.get_doc("MS Term Submission", name) if name else None


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def reopen_for_appeal(
	student_group: str = None,
	course: str = None,
	academic_term: str = None,
	reason: str = None,
	persona: str = None,
):
	"""Hand one course of one section back to its teacher after an appeal.

	Deliberately narrow. Reopening a whole term would unlock every subject and
	invite exactly the accident the school is trying to avoid — a teacher
	editing marks that were never in dispute.
	"""
	if not student_group or not course:
		return fail(
			message_en="A class and a course are required.",
			message_ar="يجب تحديد الشعبة والمادة.",
		)
	if not (reason or "").strip():
		# The reason is the point: months later, "why is this mark different
		# from the printed report?" must have an answer.
		return fail(
			message_en="A reason is required to reopen marks.",
			message_ar="يجب كتابة سبب إعادة الفتح (مثال: طعن الطالب على العلامة).",
		)

	academic_term = academic_term or get_default_academic_term()
	doc = _submission(student_group, course, academic_term)
	if not doc:
		return fail(
			message_en="These marks have not been submitted yet.",
			message_ar="لم يتم إرسال علامات هذه المادة بعد — لا حاجة لإعادة الفتح.",
		)
	if doc.status not in ("Submitted", "Approved", "Published"):
		return fail(
			message_en="These marks are already open to the teacher.",
			message_ar="العلامات مفتوحة للمعلم بالفعل.",
		)

	was_published = doc.status == "Published"

	doc.status = "Draft"
	doc.ms_reopened_on = now()
	doc.ms_reopened_by = frappe.session.user
	doc.ms_reopen_reason = reason.strip()
	# Clearing these keeps the record honest: the term is no longer approved
	# or published, and a later republish records who did it and when.
	doc.reviewed_by = None
	doc.reviewed_on = None
	doc.published_by = None
	doc.published_on = None
	doc.save(ignore_permissions=True)

	# Marks that were visible to families are pulled back while under appeal:
	# a family should not read a mark that the school has agreed to re-examine.
	hidden = 0
	if was_published:
		entries = frappe.get_all(
			"MS Gradebook Entry",
			filters={
				"student_group": student_group,
				"course": course,
				**({"academic_term": academic_term} if academic_term else {}),
			},
			pluck="name",
		)
		for name in entries:
			frappe.db.set_value(
				"MS Gradebook Entry", name, "ms_is_published", 0, update_modified=False
			)
		hidden = len(entries)

	frappe.db.commit()

	return {
		"status": doc.status,
		"hidden": hidden,
		"message_ar": (
			f"تم إعادة فتح علامات {course} لشعبة {student_group} للتعديل."
			+ (f" تم إخفاء {hidden} علامة عن الطلاب مؤقتاً." if hidden else "")
		),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def change_log(
	student_group: str = None,
	course: str = None,
	academic_term: str = None,
	since: str = None,
	persona: str = None,
):
	"""Every mark that changed after it was first entered.

	Read from Frappe's Version table, which records each edit with the old and
	new value, the user and the timestamp. The frontend uses `changedStudents`
	to outline exactly the cells that moved, so a teacher correcting one appeal
	can see at a glance that they have not touched anyone else.
	"""

	# A teacher reads the history of what they teach in this class, and no
	# more: the log names students and their marks.
	from match_schools.api.gradeflow import assert_teacher_teaches

	if student_group and course:
		assert_teacher_teaches(persona, student_group, course)
	if not student_group or not course:
		return fail(
			message_en="A class and a course are required.",
			message_ar="يجب تحديد الشعبة والمادة.",
		)

	filters = {"student_group": student_group, "course": course}
	if academic_term:
		filters["academic_term"] = academic_term

	entries = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["name", "student", "student_name", "component_name", "score", "max_score"],
		limit_page_length=0,
	)
	if not entries:
		info = _reopen_info(student_group, course, academic_term)
		return {
			"changes": [],
			"changedStudents": [],
			"total": 0,
			"reopen": info,
			"since": (info or {}).get("on"),
		}

	by_name = {e.name: e for e in entries}

	# When the course was reopened for an appeal, the interesting window is
	# everything after that moment: the administration is reviewing what the
	# teacher did with the reopening, not the whole history of the term.
	reopen = _reopen_info(student_group, course, academic_term)
	version_filters = {
		"ref_doctype": "MS Gradebook Entry",
		"docname": ["in", list(by_name)],
	}
	if since or (reopen and reopen.get("on")):
		version_filters["creation"] = [">", since or reopen["on"]]

	versions = frappe.get_all(
		"Version",
		filters=version_filters,
		fields=["name", "docname", "owner", "creation", "data"],
		order_by="creation desc",
		limit_page_length=0,
	)

	changes = []
	changed_students: set[str] = set()
	for v in versions:
		try:
			payload = json.loads(v.data or "{}")
		except Exception:
			continue
		for field, old, new in payload.get("changed") or []:
			# Only the mark itself matters here; percentage moves with it and
			# would double every row.
			if field != "score":
				continue
			entry = by_name.get(v.docname)
			if not entry:
				continue
			changed_students.add(entry.student)
			changes.append(
				{
					"id": v.name,
					"student": entry.student,
					"studentName": entry.student_name,
					"component": entry.component_name,
					"from": flt(old) if old not in (None, "") else None,
					"to": flt(new) if new not in (None, "") else None,
					"maxScore": flt(entry.max_score),
					"by": v.owner,
					"at": str(v.creation),
				}
			)

	return {
		"changes": changes,
		"changedStudents": sorted(changed_students),
		"total": len(changes),
		"reopen": reopen,
		# What the window actually covers, so the screen can say "since the
		# reopening" rather than implying this is the full history.
		"since": (since or (reopen or {}).get("on")),
	}


def _reopen_info(student_group: str, course: str, academic_term: str | None) -> dict | None:
	doc = _submission(student_group, course, academic_term)
	if not doc or not doc.get("ms_reopened_on"):
		return None
	return {
		"on": str(doc.ms_reopened_on),
		"by": doc.ms_reopened_by,
		"reason": doc.ms_reopen_reason,
		"status": doc.status,
	}


# --- Scheduled release -----------------------------------------------------


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def schedule_release(
	student_group: str = None,
	course: str = None,
	component_name: str = None,
	release_on: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Set the date a component's marks become visible to families.

	Until that date the marks stay hidden however they were saved, so a
	teacher can finish marking early without the class seeing results before
	the school intends. Clearing the date returns the component to manual
	control.
	"""
	if not student_group or not course or not component_name:
		return fail(
			message_en="A class, course and component are required.",
			message_ar="يجب تحديد الشعبة والمادة والمكوّن.",
		)

	from match_schools.api.gradeflow import assert_entry_allowed, assert_teacher_owns_course

	assert_teacher_owns_course(persona, course)
	assert_entry_allowed(persona, student_group, course, academic_term)

	if release_on:
		if getdate(release_on) < getdate(nowdate()):
			return fail(
				message_en="The release date cannot be in the past.",
				message_ar="تاريخ النشر يجب أن يكون اليوم أو بعده.",
			)

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
			message_en="There are no marks for this component.",
			message_ar="لا توجد درجات في هذا المكوّن.",
		)

	for name in names:
		values = {"ms_release_on": getdate(release_on) if release_on else None}
		# Setting a future date hides the marks until it arrives; a date of
		# today releases them immediately rather than waiting for the job.
		if release_on:
			values["ms_is_published"] = 1 if getdate(release_on) <= getdate(nowdate()) else 0
		frappe.db.set_value("MS Gradebook Entry", name, values, update_modified=False)

	frappe.db.commit()

	return {
		"releaseOn": str(getdate(release_on)) if release_on else None,
		"count": len(names),
		"message_ar": (
			f"سيتم عرض {len(names)} درجة لأولياء الأمور بتاريخ {getdate(release_on)}."
			if release_on
			else "تم إلغاء موعد النشر — التحكم يدوي الآن."
		),
	}


def publish_due_marks():
	"""Publish marks whose release date has arrived.

	Runs daily from the scheduler. Idempotent: a mark already published is not
	touched, so a second run in the same day changes nothing.
	"""
	today = getdate(nowdate())
	names = frappe.get_all(
		"MS Gradebook Entry",
		filters={
			"ms_release_on": ["<=", today],
			"ms_is_published": 0,
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(
			"MS Gradebook Entry",
			name,
			{"ms_is_published": 1, "ms_published_on": now()},
			update_modified=False,
		)

	# Term results scheduled for release follow the same rule.
	submissions = frappe.get_all(
		"MS Term Submission",
		filters={"ms_release_on": ["<=", today], "status": "Approved"},
		pluck="name",
	)
	for name in submissions:
		doc = frappe.get_doc("MS Term Submission", name)
		doc.status = "Published"
		doc.published_on = now()
		doc.save(ignore_permissions=True)

	if names or submissions:
		frappe.db.commit()

	return {"marks": len(names), "submissions": len(submissions)}
