# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Online quizzes with automatic marking.

A teacher writes questions, publishes the quiz to a class within a time
window, and students sit it in the browser. Multiple-choice and true/false
answers are marked on submission; short answers are matched case-insensitively
against the expected text and flagged for review when they do not match, so a
human decides rather than the student losing a mark to a typo.

The correct answers are never sent to a student while the quiz is live — the
question payload they receive is deliberately stripped.
"""

import random

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime, today

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
	paginate,
	parse_json_arg,
	resolve_scope,
)

STATUS_AR = {"Draft": "مسودة", "Published": "منشور", "Closed": "مغلق"}
ATTEMPT_AR = {"In Progress": "قيد الحل", "Submitted": "مُسلّم", "Graded": "مُصحّح"}
TYPE_AR = {
	"Multiple Choice": "اختيار من متعدد",
	"True/False": "صح أو خطأ",
	"Short Answer": "إجابة قصيرة",
}


def _groups_of(scope: dict) -> list[str]:
	students = scope.get("students") or []
	if not students:
		return []
	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", students], "parenttype": "Student Group", "active": 1},
				fields=["parent"],
			)
		}
	)


def _is_open(quiz) -> bool:
	"""Published, and inside its time window if one is set."""
	if quiz.status != "Published":
		return False
	now = now_datetime()
	if quiz.opens_on and get_datetime(quiz.opens_on) > now:
		return False
	if quiz.closes_on and get_datetime(quiz.closes_on) < now:
		return False
	return True


def _assert_teacher_owns(persona: str, course: str):
	if persona in BACK_OFFICE:
		return
	from match_schools.api.gradeflow import assert_teacher_owns_course

	assert_teacher_owns_course(persona, course)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_quizzes(
	student_group: str = None,
	course: str = None,
	status: str = None,
	page: int = 1,
	page_size: int = 20,
	persona: str = None,
):
	"""Quizzes visible to the caller."""
	scope = resolve_scope(persona)
	filters = {}

	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import courses_of_instructor

		courses = courses_of_instructor(scope.get("instructor"))
		if not courses:
			return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}
		filters["course"] = ["in", sorted(courses)]
	elif persona in (ROLE_STUDENT, ROLE_PARENT):
		groups = _groups_of(scope)
		if not groups:
			return {"items": [], "total": 0, "page": 1, "page_size": cint(page_size) or 20}
		filters["student_group"] = ["in", groups]
		# A draft is the teacher's working copy.
		filters["status"] = ["in", ["Published", "Closed"]]

	if student_group:
		filters["student_group"] = student_group
	if course:
		filters["course"] = course
	if status and persona not in (ROLE_STUDENT, ROLE_PARENT):
		filters["status"] = status

	total = frappe.db.count("MS Quiz", filters)
	page, page_size, offset = paginate(page, page_size)
	rows = frappe.get_all(
		"MS Quiz",
		filters=filters,
		fields=[
			"name", "title", "course", "student_group", "status", "opens_on", "closes_on",
			"time_limit_minutes", "attempts_allowed", "total_marks", "pass_mark",
			"instructor", "show_answers_after",
		],
		order_by="modified desc",
		start=offset,
		page_length=page_size,
	)

	# Question counts and the viewer's own attempts, in one query each.
	counts: dict[str, int] = {}
	if rows:
		for q in frappe.get_all(
			"MS Quiz Question",
			filters={"parent": ["in", [r.name for r in rows]], "parenttype": "MS Quiz"},
			fields=["parent"],
		):
			counts[q.parent] = counts.get(q.parent, 0) + 1

	viewer = scope.get("student") if persona == ROLE_STUDENT else None
	my_attempts: dict[str, list] = {}
	if viewer and rows:
		for a in frappe.get_all(
			"MS Quiz Attempt",
			filters={"quiz": ["in", [r.name for r in rows]], "student": viewer},
			fields=["name", "quiz", "status", "score", "percentage", "passed", "attempt_number"],
			order_by="attempt_number",
		):
			my_attempts.setdefault(a.quiz, []).append(
				{
					"id": a.name,
					"status": a.status,
					"status_label": ATTEMPT_AR.get(a.status, a.status),
					"score": flt(a.score),
					"percentage": flt(a.percentage),
					"passed": bool(a.passed),
					"attempt": cint(a.attempt_number),
				}
			)

	# For staff: how many students have sat each quiz.
	submissions: dict[str, int] = {}
	if rows and persona not in (ROLE_STUDENT, ROLE_PARENT):
		for a in frappe.get_all(
			"MS Quiz Attempt",
			filters={
				"quiz": ["in", [r.name for r in rows]],
				"status": ["in", ["Submitted", "Graded"]],
			},
			fields=["quiz"],
		):
			submissions[a.quiz] = submissions.get(a.quiz, 0) + 1

	items = []
	for r in rows:
		attempts = my_attempts.get(r.name, [])
		allowed = cint(r.attempts_allowed) or 1
		items.append(
			{
				"id": r.name,
				"title": r.title,
				"course": r.course,
				"student_group": r.student_group,
				"status": r.status,
				"status_label": STATUS_AR.get(r.status, r.status),
				"opens_on": str(r.opens_on or ""),
				"closes_on": str(r.closes_on or ""),
				"time_limit": cint(r.time_limit_minutes),
				"attempts_allowed": allowed,
				"total_marks": flt(r.total_marks),
				"pass_mark": flt(r.pass_mark),
				"questions": counts.get(r.name, 0),
				"instructor": r.instructor,
				"open": _is_open(frappe._dict(r)),
				"submissions": submissions.get(r.name, 0),
				"my_attempts": attempts,
				"attempts_left": max(allowed - len(attempts), 0),
				"best": max((a["percentage"] for a in attempts), default=None),
			}
		)

	return {"items": items, "total": total, "page": page, "page_size": page_size}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_quiz(payload: str | dict, persona: str = None):
	"""Create or update a quiz."""
	data = parse_json_arg(payload) or {}
	for field in ("title", "course", "student_group"):
		if not data.get(field):
			return fail(
				message_en=f"{field} is required.",
				message_ar="العنوان والمادة والشعبة مطلوبة.",
			)

	_assert_teacher_owns(persona, data["course"])

	quiz_id = data.get("id")
	doc = frappe.get_doc("MS Quiz", quiz_id) if quiz_id else frappe.new_doc("MS Quiz")

	# Editing a live quiz would change the paper under students already sitting it.
	if quiz_id and doc.status == "Published" and data.get("questions") is not None:
		if frappe.db.exists("MS Quiz Attempt", {"quiz": quiz_id}):
			return fail(
				message_en="Students have already started; the questions cannot be changed.",
				message_ar="بدأ الطلاب بالحل — لا يمكن تعديل الأسئلة الآن.",
			)

	for field in (
		"title", "course", "student_group", "status", "opens_on", "closes_on",
		"instructions", "show_answers_after",
	):
		if data.get(field) is not None:
			setattr(doc, field, data[field])

	doc.time_limit_minutes = cint(data.get("time_limit_minutes")) or 20
	doc.attempts_allowed = cint(data.get("attempts_allowed")) or 1
	doc.shuffle_questions = cint(data.get("shuffle_questions", 1))
	doc.pass_mark = flt(data.get("pass_mark")) or 50
	doc.academic_year = data.get("academic_year") or get_default_academic_year()
	doc.academic_term = data.get("academic_term") or get_default_academic_term()

	if persona == ROLE_TEACHER and not doc.instructor:
		doc.instructor = resolve_scope(persona).get("instructor")

	if data.get("questions") is not None:
		doc.set("questions", [])
		for q in data["questions"]:
			if not q.get("question_text") or not q.get("correct_answer"):
				continue
			doc.append(
				"questions",
				{
					"question_text": q["question_text"],
					"question_type": q.get("question_type") or "Multiple Choice",
					"marks": flt(q.get("marks")) or 1,
					"option_a": q.get("option_a"),
					"option_b": q.get("option_b"),
					"option_c": q.get("option_c"),
					"option_d": q.get("option_d"),
					"correct_answer": str(q["correct_answer"]),
					"explanation": q.get("explanation"),
				},
			)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {
			"id": doc.name,
			"title": doc.title,
			"questions": len(doc.questions),
			"total_marks": flt(doc.total_marks),
			"status": doc.status,
		},
		"message_en": "Quiz saved.",
		"message_ar": "تم حفظ الاختبار.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def get_quiz(quiz: str, persona: str = None):
	"""The full quiz including answers — staff only."""
	doc = frappe.get_doc("MS Quiz", quiz)
	_assert_teacher_owns(persona, doc.course)

	return {
		"id": doc.name,
		"title": doc.title,
		"course": doc.course,
		"student_group": doc.student_group,
		"status": doc.status,
		"status_label": STATUS_AR.get(doc.status, doc.status),
		"opens_on": str(doc.opens_on or ""),
		"closes_on": str(doc.closes_on or ""),
		"time_limit": cint(doc.time_limit_minutes),
		"attempts_allowed": cint(doc.attempts_allowed),
		"shuffle": bool(doc.shuffle_questions),
		"total_marks": flt(doc.total_marks),
		"pass_mark": flt(doc.pass_mark),
		"show_answers_after": doc.show_answers_after,
		"instructions": doc.instructions,
		"questions": [
			{
				"idx": q.idx,
				"question_text": q.question_text,
				"question_type": q.question_type,
				"type_label": TYPE_AR.get(q.question_type, q.question_type),
				"marks": flt(q.marks),
				"option_a": q.option_a,
				"option_b": q.option_b,
				"option_c": q.option_c,
				"option_d": q.option_d,
				"correct_answer": q.correct_answer,
				"explanation": q.explanation,
			}
			for q in doc.questions
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_quiz(quiz: str, persona: str = None):
	if frappe.db.exists("MS Quiz Attempt", {"quiz": quiz}):
		return fail(
			message_en="Students have sat this quiz; close it instead of deleting.",
			message_ar="توجد محاولات على هذا الاختبار — أغلقه بدل حذفه.",
		)
	frappe.delete_doc("MS Quiz", quiz, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": quiz},
		"message_en": "Deleted.",
		"message_ar": "تم حذف الاختبار.",
	}


# --- Sitting the quiz ------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_STUDENT)
def start_attempt(quiz: str, persona: str = None):
	"""Begin an attempt and return the paper — without the answers."""
	scope = resolve_scope(persona)
	student = scope.get("student")
	if not student:
		return fail(
			message_en="No student is linked to your account.",
			message_ar="لا يوجد طالب مرتبط بحسابك.",
		)

	doc = frappe.get_doc("MS Quiz", quiz)

	if doc.student_group not in _groups_of(scope):
		frappe.throw(_("This quiz is not for your class."), frappe.PermissionError)
	if not _is_open(doc):
		return fail(
			message_en="This quiz is not open.",
			message_ar="الاختبار غير متاح حالياً — تحقق من موعد الفتح والإغلاق.",
		)

	previous = frappe.get_all(
		"MS Quiz Attempt",
		filters={"quiz": quiz, "student": student},
		fields=["name", "status", "attempt_number"],
		order_by="attempt_number desc",
	)

	# Resume an unfinished attempt rather than burning another one.
	live = next((a for a in previous if a.status == "In Progress"), None)
	if live:
		attempt = frappe.get_doc("MS Quiz Attempt", live.name)
	else:
		allowed = cint(doc.attempts_allowed) or 1
		if len(previous) >= allowed:
			return fail(
				message_en=f"You have used all {allowed} attempt(s).",
				message_ar=f"استنفدت عدد المحاولات المسموح بها ({allowed}).",
			)
		attempt = frappe.new_doc("MS Quiz Attempt")
		attempt.quiz = quiz
		attempt.student = student
		attempt.attempt_number = len(previous) + 1
		attempt.status = "In Progress"
		attempt.started_on = now_datetime()
		attempt.total_marks = flt(doc.total_marks)
		attempt.insert(ignore_permissions=True)
		frappe.db.commit()

	# The paper the student sees never carries correct_answer or explanation.
	questions = [
		{
			"idx": q.idx,
			"question_text": q.question_text,
			"question_type": q.question_type,
			"type_label": TYPE_AR.get(q.question_type, q.question_type),
			"marks": flt(q.marks),
			"options": [
				{"key": key, "text": text}
				for key, text in (
					("A", q.option_a), ("B", q.option_b), ("C", q.option_c), ("D", q.option_d)
				)
				if text
			],
		}
		for q in doc.questions
	]
	if doc.shuffle_questions:
		random.shuffle(questions)

	return {
		"attempt": attempt.name,
		"quiz": doc.name,
		"title": doc.title,
		"instructions": doc.instructions,
		"time_limit": cint(doc.time_limit_minutes),
		"total_marks": flt(doc.total_marks),
		"started_on": str(attempt.started_on or ""),
		"attempt_number": cint(attempt.attempt_number),
		"questions": questions,
	}


def _normalise(text: str) -> str:
	"""Fold case, spacing and Arabic variants before comparing a short answer."""
	value = (text or "").strip().lower()
	value = value.replace("ـ", "")
	for variant in "أإآ":
		value = value.replace(variant, "ا")
	value = value.replace("ى", "ي").replace("ة", "ه")
	return " ".join(value.split())


@frappe.whitelist()
@ms_endpoint(ROLE_STUDENT)
def submit_attempt(attempt: str, answers: str | list, persona: str = None):
	"""Mark the attempt and return the result."""
	scope = resolve_scope(persona)
	doc = frappe.get_doc("MS Quiz Attempt", attempt)

	if doc.student != scope.get("student"):
		frappe.throw(_("This is not your attempt."), frappe.PermissionError)
	if doc.status != "In Progress":
		return fail(
			message_en="This attempt has already been submitted.",
			message_ar="تم تسليم هذه المحاولة بالفعل.",
		)

	quiz = frappe.get_doc("MS Quiz", doc.quiz)
	given = {cint(a.get("idx")): str(a.get("answer") or "") for a in (parse_json_arg(answers, []) or [])}

	score = 0.0
	needs_review = False
	doc.set("answers", [])

	for q in quiz.questions:
		answer = given.get(q.idx, "")
		possible = flt(q.marks)
		correct = False
		awarded = 0.0

		if q.question_type == "Short Answer":
			# A typo should not silently cost a mark, so a mismatch is flagged
			# for the teacher rather than marked wrong outright.
			if _normalise(answer) == _normalise(q.correct_answer):
				correct, awarded = True, possible
			elif answer.strip():
				needs_review = True
		else:
			correct = _normalise(answer) == _normalise(q.correct_answer)
			awarded = possible if correct else 0.0

		score += awarded
		doc.append(
			"answers",
			{
				"question_idx": q.idx,
				"question_text": q.question_text,
				"given_answer": answer,
				"correct_answer": q.correct_answer,
				"is_correct": cint(correct),
				"marks_awarded": awarded,
				"marks_possible": possible,
			},
		)

	total = flt(quiz.total_marks) or sum(flt(q.marks) for q in quiz.questions)
	doc.score = round(score, 2)
	doc.total_marks = total
	doc.percentage = round(score / total * 100, 1) if total else 0.0
	doc.passed = cint(doc.percentage >= flt(quiz.pass_mark))
	doc.needs_review = cint(needs_review)
	doc.status = "Submitted" if needs_review else "Graded"
	doc.submitted_on = now_datetime()
	if doc.started_on:
		doc.duration_seconds = int(
			(get_datetime(doc.submitted_on) - get_datetime(doc.started_on)).total_seconds()
		)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	reveal = quiz.show_answers_after == "Immediately" or (
		quiz.show_answers_after == "After Close" and not _is_open(quiz)
	)

	return {
		"success": True,
		"data": {
			"attempt": doc.name,
			"score": flt(doc.score),
			"total": total,
			"percentage": flt(doc.percentage),
			"passed": bool(doc.passed),
			"needs_review": bool(doc.needs_review),
			"correct": sum(1 for a in doc.answers if a.is_correct),
			"questions": len(doc.answers),
			"answers": (
				[
					{
						"idx": a.question_idx,
						"question": a.question_text,
						"given": a.given_answer,
						"correct_answer": a.correct_answer,
						"is_correct": bool(a.is_correct),
						"marks": flt(a.marks_awarded),
						"possible": flt(a.marks_possible),
					}
					for a in doc.answers
				]
				if reveal
				else []
			),
			"answers_revealed": reveal,
		},
		"message_en": "Submitted.",
		"message_ar": (
			"تم التسليم — بانتظار تصحيح المعلم لبعض الإجابات."
			if needs_review
			else f"تم التسليم — نتيجتك {doc.percentage}%"
		),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def quiz_results(quiz: str, persona: str = None):
	"""Everyone's attempts, plus how each question performed."""
	doc = frappe.get_doc("MS Quiz", quiz)
	_assert_teacher_owns(persona, doc.course)

	attempts = frappe.get_all(
		"MS Quiz Attempt",
		filters={"quiz": quiz},
		fields=[
			"name", "student", "student_name", "attempt_number", "status",
			"score", "total_marks", "percentage", "passed", "needs_review",
			"submitted_on", "duration_seconds",
		],
		order_by="percentage desc",
	)

	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": doc.student_group, "parenttype": "Student Group", "active": 1},
		fields=["student", "student_name"],
	)
	sat = {a.student for a in attempts}

	# Which questions the class found hardest — the useful signal for a teacher.
	stats: dict[int, dict] = {}
	if attempts:
		for a in frappe.get_all(
			"MS Quiz Answer",
			filters={
				"parent": ["in", [x.name for x in attempts]],
				"parenttype": "MS Quiz Attempt",
			},
			fields=["question_idx", "question_text", "is_correct"],
		):
			bucket = stats.setdefault(
				a.question_idx, {"text": a.question_text, "correct": 0, "total": 0}
			)
			bucket["total"] += 1
			bucket["correct"] += 1 if a.is_correct else 0

	scored = [flt(a.percentage) for a in attempts if a.status in ("Submitted", "Graded")]

	return {
		"quiz": {
			"id": doc.name,
			"title": doc.title,
			"course": doc.course,
			"student_group": doc.student_group,
			"total_marks": flt(doc.total_marks),
			"pass_mark": flt(doc.pass_mark),
			"status": doc.status,
		},
		"summary": {
			"roster": len(roster),
			"sat": len(sat),
			"not_sat": len(roster) - len(sat),
			"average": round(sum(scored) / len(scored), 1) if scored else None,
			"passed": sum(1 for a in attempts if a.passed),
			"needs_review": sum(1 for a in attempts if a.needs_review),
		},
		"attempts": [
			{
				"id": a.name,
				"student": a.student,
				"student_name": a.student_name,
				"attempt": cint(a.attempt_number),
				"status": a.status,
				"status_label": ATTEMPT_AR.get(a.status, a.status),
				"score": flt(a.score),
				"total": flt(a.total_marks),
				"percentage": flt(a.percentage),
				"passed": bool(a.passed),
				"needs_review": bool(a.needs_review),
				"submitted_on": str(a.submitted_on or ""),
				"minutes": round(cint(a.duration_seconds) / 60, 1),
			}
			for a in attempts
		],
		"not_sat": [
			{"student": r.student, "student_name": r.student_name}
			for r in roster
			if r.student not in sat
		],
		"questions": [
			{
				"idx": idx,
				"text": s["text"],
				"correct": s["correct"],
				"total": s["total"],
				"percent": round(s["correct"] / s["total"] * 100, 1) if s["total"] else 0,
			}
			for idx, s in sorted(stats.items())
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def review_attempt(attempt: str, marks: str | list = None, persona: str = None):
	"""Adjust the marks on answers the system could not decide.

	`marks` is [{idx, marks_awarded}] — usually the short answers a student
	phrased differently from the expected text.
	"""
	doc = frappe.get_doc("MS Quiz Attempt", attempt)
	quiz = frappe.get_doc("MS Quiz", doc.quiz)
	_assert_teacher_owns(persona, quiz.course)

	updates = {
		cint(m.get("idx")): flt(m.get("marks_awarded"))
		for m in (parse_json_arg(marks, []) or [])
	}

	score = 0.0
	for answer in doc.answers:
		if answer.question_idx in updates:
			awarded = min(max(updates[answer.question_idx], 0), flt(answer.marks_possible))
			answer.marks_awarded = awarded
			answer.is_correct = cint(awarded >= flt(answer.marks_possible))
		score += flt(answer.marks_awarded)

	total = flt(doc.total_marks) or 1
	doc.score = round(score, 2)
	doc.percentage = round(score / total * 100, 1)
	doc.passed = cint(doc.percentage >= flt(quiz.pass_mark))
	doc.needs_review = 0
	doc.status = "Graded"
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {
			"attempt": doc.name,
			"score": flt(doc.score),
			"percentage": flt(doc.percentage),
			"passed": bool(doc.passed),
		},
		"message_en": "Marks updated.",
		"message_ar": f"تم التصحيح — النتيجة {doc.percentage}%",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_attempt(attempt: str, persona: str = None):
	"""One attempt's detail, with answers shown only when allowed."""
	doc = frappe.get_doc("MS Quiz Attempt", attempt)
	quiz = frappe.get_doc("MS Quiz", doc.quiz)
	scope = resolve_scope(persona)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		if doc.student not in (scope.get("students") or []):
			frappe.throw(_("This is not your attempt."), frappe.PermissionError)
		reveal = quiz.show_answers_after == "Immediately" or (
			quiz.show_answers_after == "After Close" and not _is_open(quiz)
		)
	else:
		_assert_teacher_owns(persona, quiz.course)
		reveal = True

	return {
		"id": doc.name,
		"quiz": doc.quiz,
		"quiz_title": quiz.title,
		"student": doc.student,
		"student_name": doc.student_name,
		"status": doc.status,
		"status_label": ATTEMPT_AR.get(doc.status, doc.status),
		"score": flt(doc.score),
		"total": flt(doc.total_marks),
		"percentage": flt(doc.percentage),
		"passed": bool(doc.passed),
		"needs_review": bool(doc.needs_review),
		"submitted_on": str(doc.submitted_on or ""),
		"minutes": round(cint(doc.duration_seconds) / 60, 1),
		"answers_revealed": reveal,
		"answers": (
			[
				{
					"idx": a.question_idx,
					"question": a.question_text,
					"given": a.given_answer,
					"correct_answer": a.correct_answer,
					"is_correct": bool(a.is_correct),
					"marks": flt(a.marks_awarded),
					"possible": flt(a.marks_possible),
				}
				for a in doc.answers
			]
			if reveal
			else []
		),
	}
