# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Teacher appraisal: lesson observations and the performance file.

An observation is a scored visit to a lesson. It stays a Draft while the
observer writes it, becomes Shared when it is released to the teacher, and
Acknowledged once the teacher has read and responded.

A teacher sees only their own observations, and only after they are shared —
a draft is the observer's working note, not feedback. The performance file
pulls the observations together with what the system already knows about the
teacher's classes.
"""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, now_datetime, today

from match_k12.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	k12_endpoint,
	paginate,
	parse_json_arg,
	resolve_scope,
)

TYPE_AR = {
	"Lesson Observation": "زيارة صفية",
	"Peer Review": "تقييم الأقران",
	"Annual Appraisal": "تقييم سنوي",
	"Follow-up": "زيارة متابعة",
}
STATUS_AR = {"Draft": "مسودة", "Shared": "مُرسل للمعلم", "Acknowledged": "تم الاطلاع"}

# The default rubric, so an observer starts from something usable.
DEFAULT_CRITERIA = [
	("التخطيط للدرس", 1, 5),
	("إدارة الصف", 1, 5),
	("وضوح الشرح", 1.5, 5),
	("تفاعل الطلاب", 1.5, 5),
	("استخدام الوسائل التعليمية", 1, 5),
	("التقويم ومتابعة الفهم", 1, 5),
	("الالتزام بالوقت", 0.5, 5),
]


def _visible_filters(persona: str, scope: dict) -> dict:
	"""A teacher sees only their own, and only once shared."""
	if persona in BACK_OFFICE:
		return {}
	instructor = scope.get("instructor")
	if not instructor:
		return {"instructor": "__none__"}
	return {"instructor": instructor, "status": ["in", ["Shared", "Acknowledged"]]}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def list_observations(
	instructor: str = None,
	status: str = None,
	page: int = 1,
	page_size: int = 20,
	persona: str = None,
):
	"""Observations visible to the caller."""
	scope = resolve_scope(persona)
	filters = _visible_filters(persona, scope)

	if instructor and persona in BACK_OFFICE:
		filters["instructor"] = instructor
	if status and persona in BACK_OFFICE:
		filters["status"] = status

	total = frappe.db.count("K12 Teacher Observation", filters)
	page, page_size, offset = paginate(page, page_size)
	rows = frappe.get_all(
		"K12 Teacher Observation",
		filters=filters,
		fields=[
			"name", "instructor", "instructor_name", "observation_date",
			"observation_type", "status", "course", "student_group",
			"overall_percent", "rating", "observer", "teacher_response",
		],
		order_by="observation_date desc",
		start=offset,
		page_length=page_size,
	)

	return {
		"items": [
			{
				"id": r.name,
				"instructor": r.instructor,
				"instructor_name": r.instructor_name,
				"date": str(r.observation_date or ""),
				"type": r.observation_type,
				"type_label": TYPE_AR.get(r.observation_type, r.observation_type),
				"status": r.status,
				"status_label": STATUS_AR.get(r.status, r.status),
				"course": r.course,
				"student_group": r.student_group,
				"percent": flt(r.overall_percent),
				"rating": r.rating,
				"observer": r.observer,
				"has_response": bool(r.teacher_response),
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_observation(observation: str, persona: str = None):
	"""One observation with its criteria."""
	doc = frappe.get_doc("K12 Teacher Observation", observation)

	if persona == ROLE_TEACHER:
		scope = resolve_scope(persona)
		if doc.instructor != scope.get("instructor"):
			frappe.throw(_("This is not your observation."), frappe.PermissionError)
		if doc.status == "Draft":
			frappe.throw(
				_("This observation has not been shared with you yet."), frappe.PermissionError
			)

	return {
		"id": doc.name,
		"instructor": doc.instructor,
		"instructor_name": doc.instructor_name,
		"date": str(doc.observation_date or ""),
		"type": doc.observation_type,
		"type_label": TYPE_AR.get(doc.observation_type, doc.observation_type),
		"status": doc.status,
		"status_label": STATUS_AR.get(doc.status, doc.status),
		"course": doc.course,
		"student_group": doc.student_group,
		"academic_year": doc.academic_year,
		"academic_term": doc.academic_term,
		"observer": doc.observer,
		"percent": flt(doc.overall_percent),
		"score": flt(doc.overall_score),
		"rating": doc.rating,
		"strengths": doc.strengths,
		"improvements": doc.improvements,
		"action_plan": doc.action_plan,
		"teacher_response": doc.teacher_response,
		"criteria": [
			{
				"criterion": c.criterion,
				"weight": flt(c.weight),
				"score": flt(c.score),
				"max_score": flt(c.max_score),
				"comment": c.comment,
			}
			for c in doc.criteria
		],
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_observation(payload: str | dict, persona: str = None):
	"""Create or update an observation. Only the administration observes."""
	data = parse_json_arg(payload) or {}
	if not data.get("instructor"):
		return fail(message_en="Choose a teacher.", message_ar="اختر المعلم.")
	if not data.get("observation_date"):
		return fail(message_en="A date is required.", message_ar="تاريخ الزيارة مطلوب.")

	observation_id = data.get("id")
	doc = (
		frappe.get_doc("K12 Teacher Observation", observation_id)
		if observation_id
		else frappe.new_doc("K12 Teacher Observation")
	)

	if doc.status == "Acknowledged":
		return fail(
			message_en="An acknowledged observation cannot be edited.",
			message_ar="لا يمكن تعديل تقييم اطّلع عليه المعلم.",
		)

	for field in (
		"instructor", "observation_date", "observation_type", "course",
		"student_group", "strengths", "improvements", "action_plan",
	):
		if data.get(field) is not None:
			setattr(doc, field, data[field])

	doc.academic_year = data.get("academic_year") or get_default_academic_year()
	doc.academic_term = data.get("academic_term") or get_default_academic_term()
	doc.observer = doc.observer or frappe.session.user

	criteria = data.get("criteria")
	if criteria is None and not doc.criteria:
		criteria = [
			{"criterion": name, "weight": weight, "max_score": out_of, "score": 0}
			for name, weight, out_of in DEFAULT_CRITERIA
		]
	if criteria is not None:
		doc.set("criteria", [])
		for c in criteria:
			if not c.get("criterion"):
				continue
			doc.append(
				"criteria",
				{
					"criterion": c["criterion"],
					"weight": flt(c.get("weight")) or 1,
					"score": flt(c.get("score")),
					"max_score": flt(c.get("max_score")) or 5,
					"comment": c.get("comment"),
				},
			)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {
			"id": doc.name,
			"percent": flt(doc.overall_percent),
			"rating": doc.rating,
			"status": doc.status,
		},
		"message_en": "Observation saved.",
		"message_ar": "تم حفظ التقييم.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def share_observation(observation: str, persona: str = None):
	"""Release the observation to the teacher."""
	doc = frappe.get_doc("K12 Teacher Observation", observation)
	if doc.status != "Draft":
		return fail(
			message_en="This observation has already been shared.",
			message_ar="تم إرسال هذا التقييم للمعلم بالفعل.",
		)
	if not doc.criteria:
		return fail(
			message_en="Score the criteria before sharing.",
			message_ar="أدخل درجات معايير التقييم قبل الإرسال.",
		)

	doc.status = "Shared"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "status": doc.status},
		"message_en": "Shared with the teacher.",
		"message_ar": "تم إرسال التقييم للمعلم.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_TEACHER)
def acknowledge(observation: str, response: str = None, persona: str = None):
	"""The teacher confirms they have read it, and may reply."""
	doc = frappe.get_doc("K12 Teacher Observation", observation)
	scope = resolve_scope(persona)

	if doc.instructor != scope.get("instructor"):
		frappe.throw(_("This is not your observation."), frappe.PermissionError)
	if doc.status == "Draft":
		frappe.throw(_("This observation has not been shared yet."), frappe.PermissionError)

	doc.status = "Acknowledged"
	if response:
		doc.teacher_response = response
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "status": doc.status},
		"message_en": "Acknowledged.",
		"message_ar": "تم تسجيل اطّلاعك على التقييم.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_observation(observation: str, persona: str = None):
	status = frappe.db.get_value("K12 Teacher Observation", observation, "status")
	if status == "Acknowledged":
		return fail(
			message_en="An acknowledged observation cannot be deleted.",
			message_ar="لا يمكن حذف تقييم اطّلع عليه المعلم.",
		)
	frappe.delete_doc("K12 Teacher Observation", observation, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": observation},
		"message_en": "Deleted.",
		"message_ar": "تم حذف التقييم.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def performance_file(instructor: str = None, persona: str = None):
	"""A teacher's file: observations plus what the system already knows.

	The teaching figures are drawn from records the teacher already owns —
	classes, marks entered, assignments graded — rather than asking anyone to
	fill in a form.
	"""
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
	if not instructor:
		return fail(message_en="No teacher given.", message_ar="لم يتم تحديد المعلم.")

	row = (
		frappe.db.get_value(
			"Instructor", instructor, ["name", "instructor_name", "department"], as_dict=True
		)
		or {}
	)

	filters = {"instructor": instructor}
	if persona == ROLE_TEACHER:
		# A draft is the observer's working note, not feedback.
		filters["status"] = ["in", ["Shared", "Acknowledged"]]

	observations = frappe.get_all(
		"K12 Teacher Observation",
		filters=filters,
		fields=[
			"name", "observation_date", "observation_type", "status",
			"overall_percent", "rating", "course", "student_group",
			"strengths", "improvements",
		],
		order_by="observation_date desc",
		limit=50,
	)

	scored = [flt(o.overall_percent) for o in observations if o.overall_percent]
	average = round(sum(scored) / len(scored), 1) if scored else None

	# Trend: are the scores moving up or down over the last few visits?
	trend = None
	if len(scored) >= 2:
		recent = scored[0]
		previous = scored[1]
		trend = "up" if recent > previous else "down" if recent < previous else "flat"

	groups = {
		r.parent
		for r in frappe.get_all(
			"Student Group Instructor",
			filters={"instructor": instructor, "parenttype": "Student Group"},
			fields=["parent"],
		)
	}

	from match_k12.api.gradeflow import courses_of_instructor

	courses = courses_of_instructor(instructor)

	students = 0
	if groups:
		students = frappe.db.count(
			"Student Group Student",
			{"parent": ["in", list(groups)], "parenttype": "Student Group", "active": 1},
		)

	marks_entered = frappe.db.count(
		"K12 Gradebook Entry", {"entered_by": frappe.db.get_value("Instructor", instructor, "name")}
	)
	assignments = frappe.db.count("K12 Assignment", {"instructor": instructor})
	submitted_terms = frappe.db.count(
		"K12 Term Submission",
		{"instructor": instructor, "status": ["in", ["Submitted", "Approved", "Published"]]},
	)

	return {
		"instructor": instructor,
		"name": row.get("instructor_name") or instructor,
		"department": row.get("department"),
		"summary": {
			"observations": len(observations),
			"average_percent": average,
			"latest_rating": observations[0].rating if observations else None,
			"trend": trend,
			"awaiting_response": sum(1 for o in observations if o.status == "Shared"),
		},
		"teaching": {
			"classes": len(groups),
			"subjects": len(courses),
			"students": students,
			"assignments": assignments,
			"marks_entered": marks_entered,
			"terms_submitted": submitted_terms,
		},
		"observations": [
			{
				"id": o.name,
				"date": str(o.observation_date or ""),
				"type_label": TYPE_AR.get(o.observation_type, o.observation_type),
				"status": o.status,
				"status_label": STATUS_AR.get(o.status, o.status),
				"percent": flt(o.overall_percent),
				"rating": o.rating,
				"course": o.course,
				"student_group": o.student_group,
				"strengths": o.strengths,
				"improvements": o.improvements,
			}
			for o in observations
		],
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def appraisal_overview(persona: str = None):
	"""Every teacher's standing, for the administration."""
	instructors = frappe.get_all(
		"Instructor", fields=["name", "instructor_name", "department"], limit=300
	)

	# One query for all observations, rather than one per teacher.
	by_instructor: dict[str, list] = {}
	for o in frappe.get_all(
		"K12 Teacher Observation",
		fields=["instructor", "overall_percent", "observation_date", "rating", "status"],
		order_by="observation_date desc",
		limit=2000,
	):
		by_instructor.setdefault(o.instructor, []).append(o)

	rows = []
	for i in instructors:
		items = by_instructor.get(i.name, [])
		scored = [flt(x.overall_percent) for x in items if x.overall_percent]
		rows.append(
			{
				"instructor": i.name,
				"name": i.instructor_name or i.name,
				"department": i.department,
				"observations": len(items),
				"average": round(sum(scored) / len(scored), 1) if scored else None,
				"latest": str(items[0].observation_date) if items else None,
				"latest_rating": items[0].rating if items else None,
				"drafts": sum(1 for x in items if x.status == "Draft"),
				"awaiting_response": sum(1 for x in items if x.status == "Shared"),
			}
		)

	# Teachers never observed come first — they are the ones needing a visit.
	rows.sort(key=lambda r: (r["observations"] > 0, r["average"] or 0))
	observed = [r for r in rows if r["observations"]]

	return {
		"rows": rows,
		"summary": {
			"teachers": len(rows),
			"observed": len(observed),
			"never_observed": len(rows) - len(observed),
			"school_average": (
				round(sum(r["average"] for r in observed) / len(observed), 1)
				if observed
				else None
			),
		},
		"criteria_template": [
			{"criterion": name, "weight": weight, "max_score": out_of}
			for name, weight, out_of in DEFAULT_CRITERIA
		],
	}
