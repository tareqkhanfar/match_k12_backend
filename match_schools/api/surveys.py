# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Surveys for students, teachers and parents.

The administration writes a survey, aims it at an audience, and opens it for a
period. Responses can be anonymous — and when they are, the respondent is not
stored at all rather than merely hidden, so anonymity is a property of the
data and not of the query that reads it.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, today

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

AUDIENCE_AR = {
	"Students": "الطلاب",
	"Teachers": "المعلمون",
	"Parents": "أولياء الأمور",
	"All": "الجميع",
}
STATUS_AR = {"Draft": "مسودة", "Open": "مفتوح", "Closed": "مغلق"}
TYPE_AR = {
	"Rating": "تقييم بالنجوم",
	"Single Choice": "اختيار واحد",
	"Multiple Choice": "اختيار متعدد",
	"YesNo": "نعم / لا",
	"Text": "إجابة نصية",
}

AUDIENCE_FOR_PERSONA = {
	ROLE_STUDENT: ("Students", "All"),
	ROLE_TEACHER: ("Teachers", "All"),
	ROLE_PARENT: ("Parents", "All"),
}


def _is_open(survey) -> bool:
	if survey.status != "Open":
		return False
	stamp = today()
	if survey.opens_on and str(survey.opens_on) > stamp:
		return False
	if survey.closes_on and str(survey.closes_on) < stamp:
		return False
	return True


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_surveys(status: str = None, persona: str = None):
	"""Surveys the caller may see, with whether they have already answered."""
	filters = {}
	if persona not in BACK_OFFICE:
		filters["audience"] = ["in", list(AUDIENCE_FOR_PERSONA.get(persona, ("All",)))]
		filters["status"] = "Open"
	elif status:
		filters["status"] = status

	rows = frappe.get_all(
		"MS Survey",
		filters=filters,
		fields=[
			"name", "title", "audience", "status", "anonymous", "opens_on",
			"closes_on", "intro", "response_count", "program", "student_group", "course",
		],
		order_by="modified desc",
		limit=100,
	)

	# Which of these the caller has already answered. An anonymous survey does
	# not record who responded, so it cannot be marked as done.
	answered: set[str] = set()
	if rows:
		answered = {
			r.survey
			for r in frappe.get_all(
				"MS Survey Response",
				filters={
					"survey": ["in", [x.name for x in rows]],
					"respondent": frappe.session.user,
				},
				fields=["survey"],
			)
		}

	counts: dict[str, int] = {}
	if rows and persona in BACK_OFFICE:
		for r in frappe.get_all(
			"MS Survey Response",
			filters={"survey": ["in", [x.name for x in rows]]},
			fields=["survey"],
			limit=10000,
		):
			counts[r.survey] = counts.get(r.survey, 0) + 1

	return [
		{
			"id": r.name,
			"title": r.title,
			"audience": r.audience,
			"audience_label": AUDIENCE_AR.get(r.audience, r.audience),
			"status": r.status,
			"status_label": STATUS_AR.get(r.status, r.status),
			"anonymous": bool(r.anonymous),
			"opens_on": str(r.opens_on or ""),
			"closes_on": str(r.closes_on or ""),
			"intro": r.intro,
			"responses": counts.get(r.name, cint(r.response_count)),
			"answered": r.name in answered,
			"open": _is_open(frappe._dict(r)),
		}
		for r in rows
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_survey(survey: str, persona: str = None):
	"""The questions, for answering."""
	doc = frappe.get_doc("MS Survey", survey)

	if persona not in BACK_OFFICE:
		if doc.audience not in AUDIENCE_FOR_PERSONA.get(persona, ("All",)):
			frappe.throw(_("This survey is not aimed at you."), frappe.PermissionError)
		if not _is_open(doc):
			frappe.throw(_("This survey is not open."), frappe.PermissionError)

	return {
		"id": doc.name,
		"title": doc.title,
		"audience_label": AUDIENCE_AR.get(doc.audience, doc.audience),
		"anonymous": bool(doc.anonymous),
		"intro": doc.intro,
		"closes_on": str(doc.closes_on or ""),
		"questions": [
			{
				"idx": q.idx,
				"question_text": q.question_text,
				"question_type": q.question_type,
				"type_label": TYPE_AR.get(q.question_type, q.question_type),
				"required": bool(q.required),
				"options": [
					o.strip() for o in (q.options_list or "").split("\n") if o.strip()
				],
				"scale_max": cint(q.scale_max) or 5,
			}
			for q in doc.questions
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_survey(payload: str | dict, persona: str = None):
	data = parse_json_arg(payload) or {}
	if not data.get("title"):
		return fail(message_en="A title is required.", message_ar="عنوان الاستبيان مطلوب.")

	doc = (
		frappe.get_doc("MS Survey", data["id"])
		if data.get("id")
		else frappe.new_doc("MS Survey")
	)

	# Changing the questions after people have answered would orphan their
	# answers against different questions.
	if data.get("id") and data.get("questions") is not None:
		if frappe.db.exists("MS Survey Response", {"survey": data["id"]}):
			return fail(
				message_en="Responses have been submitted; the questions cannot change.",
				message_ar="تم استلام إجابات — لا يمكن تعديل الأسئلة الآن.",
			)

	for field in ("title", "audience", "status", "opens_on", "closes_on", "intro",
	              "program", "student_group", "course"):
		if data.get(field) is not None:
			setattr(doc, field, data[field])

	doc.anonymous = cint(data.get("anonymous", 1))
	doc.academic_year = data.get("academic_year") or get_default_academic_year()
	doc.academic_term = data.get("academic_term") or get_default_academic_term()
	doc.created_by_role = persona

	if data.get("questions") is not None:
		doc.set("questions", [])
		for q in data["questions"]:
			if not q.get("question_text"):
				continue
			options = q.get("options")
			if isinstance(options, list):
				options = "\n".join(options)
			doc.append(
				"questions",
				{
					"question_text": q["question_text"],
					"question_type": q.get("question_type") or "Rating",
					"required": cint(q.get("required", 1)),
					"options_list": options,
					"scale_max": cint(q.get("scale_max")) or 5,
				},
			)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.title, "questions": len(doc.questions)},
		"message_en": "Survey saved.",
		"message_ar": "تم حفظ الاستبيان.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_survey(survey: str, persona: str = None):
	if frappe.db.exists("MS Survey Response", {"survey": survey}):
		return fail(
			message_en="Responses exist; close the survey instead of deleting it.",
			message_ar="توجد إجابات — أغلق الاستبيان بدل حذفه.",
		)
	frappe.delete_doc("MS Survey", survey, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": survey},
		"message_en": "Deleted.",
		"message_ar": "تم حذف الاستبيان.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT, ROLE_ADMIN, ROLE_SECRETARY)
def submit_response(survey: str, answers: str | list, persona: str = None):
	"""Record one person's answers."""
	doc = frappe.get_doc("MS Survey", survey)

	if persona not in BACK_OFFICE:
		if doc.audience not in AUDIENCE_FOR_PERSONA.get(persona, ("All",)):
			frappe.throw(_("This survey is not aimed at you."), frappe.PermissionError)
	if not _is_open(doc):
		return fail(message_en="This survey is closed.", message_ar="الاستبيان مغلق.")

	# A named survey is answered once; an anonymous one cannot be checked,
	# which is the trade-off anonymity buys.
	if not doc.anonymous and frappe.db.exists(
		"MS Survey Response", {"survey": survey, "respondent": frappe.session.user}
	):
		return fail(
			message_en="You have already answered this survey.",
			message_ar="لقد أجبت على هذا الاستبيان بالفعل.",
		)

	given = {cint(a.get("idx")): a for a in (parse_json_arg(answers, []) or [])}

	missing = [
		q.idx
		for q in doc.questions
		if q.required and not str(given.get(q.idx, {}).get("answer") or "").strip()
	]
	if missing:
		return fail(
			message_en=f"{len(missing)} required question(s) unanswered.",
			message_ar=f"يرجى الإجابة عن {len(missing)} سؤال إلزامي.",
		)

	response = frappe.new_doc("MS Survey Response")
	response.survey = survey
	# Anonymity is enforced by not storing the identity at all.
	response.respondent = None if doc.anonymous else frappe.session.user
	response.respondent_role = persona
	response.submitted_on = now_datetime()

	for q in doc.questions:
		a = given.get(q.idx, {})
		answer = str(a.get("answer") or "")
		response.append(
			"answers",
			{
				"question_idx": q.idx,
				"question_text": q.question_text,
				"answer": answer,
				"rating_value": flt(answer) if q.question_type == "Rating" else 0,
			},
		)

	response.insert(ignore_permissions=True)
	frappe.db.sql(
		"UPDATE `tabMS Survey` SET response_count = COALESCE(response_count, 0) + 1 WHERE name = %s",
		survey,
	)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": response.name},
		"message_en": "Thank you.",
		"message_ar": "شكراً لك — تم استلام إجابتك.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def survey_results(survey: str, persona: str = None):
	"""Aggregated results, per question."""
	doc = frappe.get_doc("MS Survey", survey)
	responses = frappe.get_all(
		"MS Survey Response",
		filters={"survey": survey},
		fields=["name", "respondent_role", "submitted_on"],
		limit=5000,
	)
	if not responses:
		return {
			"survey": {"id": doc.name, "title": doc.title, "anonymous": bool(doc.anonymous)},
			"summary": {"responses": 0, "by_role": []},
			"questions": [],
		}

	answers = frappe.get_all(
		"MS Survey Answer",
		filters={
			"parent": ["in", [r.name for r in responses]],
			"parenttype": "MS Survey Response",
		},
		fields=["question_idx", "answer", "rating_value"],
		limit=50000,
	)

	by_question: dict[int, list] = {}
	for a in answers:
		by_question.setdefault(a.question_idx, []).append(a)

	results = []
	for q in doc.questions:
		rows = by_question.get(q.idx, [])
		entry = {
			"idx": q.idx,
			"question": q.question_text,
			"type": q.question_type,
			"type_label": TYPE_AR.get(q.question_type, q.question_type),
			"answered": len(rows),
		}

		if q.question_type == "Rating":
			values = [flt(r.rating_value) for r in rows if flt(r.rating_value)]
			entry["average"] = round(sum(values) / len(values), 2) if values else None
			entry["scale_max"] = cint(q.scale_max) or 5
			# The spread matters as much as the mean for a rating question.
			entry["distribution"] = [
				{"value": v, "count": sum(1 for x in values if int(x) == v)}
				for v in range(1, entry["scale_max"] + 1)
			]
		elif q.question_type == "Text":
			entry["responses"] = [r.answer for r in rows if (r.answer or "").strip()][:100]
		else:
			tally: dict[str, int] = {}
			for r in rows:
				for part in str(r.answer or "").split(","):
					part = part.strip()
					if part:
						tally[part] = tally.get(part, 0) + 1
			total = sum(tally.values()) or 1
			entry["options"] = [
				{"option": k, "count": v, "percent": round(v / total * 100, 1)}
				for k, v in sorted(tally.items(), key=lambda x: -x[1])
			]

		results.append(entry)

	by_role: dict[str, int] = {}
	for r in responses:
		by_role[r.respondent_role or "—"] = by_role.get(r.respondent_role or "—", 0) + 1

	return {
		"survey": {
			"id": doc.name,
			"title": doc.title,
			"audience_label": AUDIENCE_AR.get(doc.audience, doc.audience),
			"anonymous": bool(doc.anonymous),
			"status": doc.status,
		},
		"summary": {
			"responses": len(responses),
			"by_role": [{"role": k, "count": v} for k, v in by_role.items()],
		},
		"questions": results,
	}
