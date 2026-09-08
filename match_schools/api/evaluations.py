"""نماذج التقييم — forms a school defines, then fills in for its pupils.

Schools do not agree on what to assess or how. One wants "الاستماع" and "الأكل
في الحصة" answered دائماً–أحياناً–أبداً; a kindergarten wants domains with a
dozen lines each; a third wants marks out of ten. So the form carries its own
criteria and its own scale, and this module reads both rather than assuming
either.

Two ways in, because both are real. One pupil at a time, from their page, when
something happened today. Or the whole class at once in a grid — the same
shape as mark entry, because a teacher filling thirty pupils across fifteen
lines will not do it thirty times through a dialog.

Answers are stored with the criterion's wording copied beside its key. Editing
a form next term would otherwise rewrite what this term's assessments appear
to have asked.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now

from match_schools.api.utils import (
	apply_period,
	fail,
	instructor_groups,
	ms_endpoint,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
ALL_ROLES = (*STAFF, ROLE_STUDENT, ROLE_PARENT)

FORM_TYPES = ["سلوك", "مهارات", "روضة", "تقرير تقدم", "مشاركة صفية", "أخرى"]
SCALE_TYPES = ["مقياس", "علامة رقمية", "نعم/لا", "نص"]

# Offered when a form is created so a teacher is not typing "دائماً / أحياناً /
# أبداً" from scratch every time. They are a starting point, not a rule: every
# option can be renamed, rescored or deleted.
SCALE_PRESETS = {
	"مقياس": [
		{"label": "دائماً", "score": 3, "tone": "success"},
		{"label": "أحياناً", "score": 2, "tone": "warning"},
		{"label": "نادراً", "score": 1, "tone": "warning"},
		{"label": "أبداً", "score": 0, "tone": "danger"},
	],
	"نعم/لا": [
		{"label": "نعم", "score": 1, "tone": "success"},
		{"label": "لا", "score": 0, "tone": "danger"},
	],
	"تقدم": [
		{"label": "متميّز", "score": 4, "tone": "success"},
		{"label": "جيد", "score": 3, "tone": "info"},
		{"label": "مقبول", "score": 2, "tone": "warning"},
		{"label": "يحتاج دعماً", "score": 1, "tone": "danger"},
	],
}

MAX_CRITERIA = 100
MAX_OPTIONS = 12


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _my_groups(persona: str) -> list[str] | None:
	"""Classes the caller may assess in. `None` means no restriction."""
	if persona in BACK_OFFICE:
		return None

	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		# مصدر واحد لكل الشاشات، ومقيَّد بالفصل المختار.
		return sorted(
			set(instructor_groups(scope.get("instructor")))
			| set(scope.get("student_groups") or [])
		)

	students = scope.get("students") or []
	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", students or [""]], "active": 1},
				fields=["parent"],
			)
		}
	)


def _assert_may_assess(persona: str, student_group: str | None) -> None:
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		frappe.throw(_("You cannot record assessments."), frappe.PermissionError)
	groups = _my_groups(persona)
	if groups is not None and student_group and student_group not in groups:
		frappe.throw(_("This class is not yours."), frappe.PermissionError)


def _form_payload(doc) -> dict:
	criteria = sorted(
		(
			{
				"key": c.name,
				"category": c.category or "",
				"item": c.item,
				"max_score": flt(c.max_score),
				"weight": flt(c.weight),
				"sort_order": cint(c.sort_order),
				"help_text": c.help_text,
			}
			for c in (doc.criteria or [])
		),
		key=lambda c: (c["sort_order"], c["category"]),
	)
	scale = sorted(
		(
			{
				"label": s.label,
				"score": flt(s.score),
				"tone": s.tone or "muted",
				"sort_order": cint(s.sort_order),
			}
			for s in (doc.scale or [])
		),
		key=lambda s: s["sort_order"],
	)
	# The grid draws a merged band per category, in the order the criteria
	# first mention them — not alphabetically, which would scramble a KG sheet.
	categories: list[str] = []
	for c in criteria:
		if c["category"] and c["category"] not in categories:
			categories.append(c["category"])

	return {
		"id": doc.name,
		"title": doc.title,
		"form_type": doc.form_type,
		"scale_type": doc.scale_type,
		"description": doc.description,
		"is_active": bool(cint(doc.is_active)),
		"allow_notes": bool(cint(doc.allow_notes)),
		"program": doc.program,
		"student_group": doc.student_group,
		"course": doc.course,
		"criteria": criteria,
		"scale": scale,
		"categories": categories,
		"max_total": sum(
			(max((s["score"] for s in scale), default=0) if doc.scale_type == "مقياس" else c["max_score"])
			for c in criteria
		),
	}


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def list_forms(form_type: str = None, student_group: str = None, persona: str = None):
	"""Forms the caller can use, newest first."""
	filters: dict = {}
	if form_type:
		filters["form_type"] = form_type
	if persona not in BACK_OFFICE:
		filters["is_active"] = 1
	apply_period(filters, "MS Evaluation Form")

	rows = frappe.get_all(
		"MS Evaluation Form",
		filters=filters,
		fields=[
			"name", "title", "form_type", "scale_type", "description", "is_active",
			"program", "student_group", "course", "created_by_user", "modified",
		],
		order_by="form_type, title",
		limit_page_length=0,
	)

	# A form tied to one class is offered only there; an untied form is the
	# school's and belongs to every list.
	if student_group:
		rows = [r for r in rows if not r.student_group or r.student_group == student_group]

	counts = {
		r.form: r.n
		for r in frappe.db.sql(
			"""select form, count(*) as n from `tabMS Evaluation Entry` group by form""",
			as_dict=True,
		)
	}
	sizes = {
		r.parent: r.n
		for r in frappe.db.sql(
			"""select parent, count(*) as n from `tabMS Evaluation Criterion`
			    where parenttype = 'MS Evaluation Form' group by parent""",
			as_dict=True,
		)
	}

	return {
		"forms": [
			{
				"id": r.name,
				"title": r.title,
				"form_type": r.form_type,
				"scale_type": r.scale_type,
				"description": r.description,
				"is_active": bool(cint(r.is_active)),
				"program": r.program,
				"student_group": r.student_group,
				"course": r.course,
				"criteria_count": sizes.get(r.name, 0),
				"entry_count": counts.get(r.name, 0),
				"modified": str(r.modified or ""),
			}
			for r in rows
		],
		"form_types": FORM_TYPES,
		"scale_types": SCALE_TYPES,
		"presets": SCALE_PRESETS,
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def get_form(form: str = None, persona: str = None):
	"""One form with its criteria and scale."""
	if not form:
		return fail(message_en="A form is required.", message_ar="يجب تحديد النموذج.")
	return _form_payload(frappe.get_doc("MS Evaluation Form", form))


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def save_form(payload: str | dict = None, persona: str = None):
	"""Create or replace a form, whole.

	Criteria and scale are sent as a complete set rather than row by row: the
	screen is one editor, and a half-applied form is a sheet that asks
	different questions than the teacher thought they wrote.
	"""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	title = (data.get("title") or "").strip()
	if not title:
		return fail(message_en="A name is required.", message_ar="اسم النموذج مطلوب.")

	form_type = data.get("form_type") or "سلوك"
	scale_type = data.get("scale_type") or "مقياس"
	if form_type not in FORM_TYPES or scale_type not in SCALE_TYPES:
		return fail(message_en="Unknown form type.", message_ar="نوع نموذج غير معروف.")

	criteria = data.get("criteria") or []
	if not criteria:
		return fail(
			message_en="Add at least one line.", message_ar="أضف بنداً واحداً على الأقل."
		)
	if len(criteria) > MAX_CRITERIA:
		return fail(
			message_en=f"At most {MAX_CRITERIA} lines.",
			message_ar=f"الحد الأقصى {MAX_CRITERIA} بنداً.",
		)

	scale = data.get("scale") or []
	if scale_type in ("مقياس", "نعم/لا"):
		if not scale:
			return fail(
				message_en="A scale needs its options.",
				message_ar="حدّد خيارات المقياس (مثل: دائماً، أحياناً، أبداً).",
			)
		if len(scale) > MAX_OPTIONS:
			return fail(
				message_en=f"At most {MAX_OPTIONS} options.",
				message_ar=f"الحد الأقصى {MAX_OPTIONS} خيارات.",
			)

	name = data.get("form")
	if name:
		doc = frappe.get_doc("MS Evaluation Form", name)
		if persona == ROLE_TEACHER and doc.created_by_user != frappe.session.user:
			frappe.throw(_("This form is not yours."), frappe.PermissionError)
		# Changing the lines of a form that has been used would rewrite what
		# past assessments asked. The answers keep their own copy of the
		# wording, so the history stays readable — but the warning is worth
		# raising to the screen.
		used = frappe.db.count("MS Evaluation Entry", {"form": name})
	else:
		doc = frappe.new_doc("MS Evaluation Form")
		doc.created_by_user = frappe.session.user
		used = 0

	doc.title = title[:180]
	doc.form_type = form_type
	doc.scale_type = scale_type
	doc.description = data.get("description")
	doc.is_active = 1 if cint(data.get("is_active", 1)) else 0
	doc.allow_notes = 1 if cint(data.get("allow_notes", 1)) else 0
	doc.program = data.get("program")
	doc.student_group = data.get("student_group")
	doc.course = data.get("course")

	doc.set("criteria", [])
	for index, row in enumerate(criteria):
		item = ((row or {}).get("item") or "").strip()
		if not item:
			continue
		doc.append(
			"criteria",
			{
				"category": (row.get("category") or "").strip()[:140],
				"item": item[:500],
				"max_score": flt(row.get("max_score")),
				"weight": flt(row.get("weight")),
				"sort_order": cint(row.get("sort_order")) or index,
				"help_text": row.get("help_text"),
			},
		)
	if not doc.criteria:
		return fail(
			message_en="Add at least one line.", message_ar="أضف بنداً واحداً على الأقل."
		)

	doc.set("scale", [])
	for index, row in enumerate(scale):
		label = ((row or {}).get("label") or "").strip()
		if not label:
			continue
		doc.append(
			"scale",
			{
				"label": label[:80],
				"score": flt(row.get("score")),
				"tone": row.get("tone") or "muted",
				"sort_order": cint(row.get("sort_order")) or index,
			},
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "used_by": used},
		"message_en": "Form saved.",
		"message_ar": (
			f"تم حفظ النموذج. ملاحظة: النموذج مستخدم في {used} تقييماً سابقاً."
			if used
			else "تم حفظ النموذج."
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def delete_form(form: str = None, persona: str = None):
	"""Remove a form that has never been used."""
	if not form:
		return fail(message_en="A form is required.", message_ar="يجب تحديد النموذج.")

	doc = frappe.get_doc("MS Evaluation Form", form)
	if persona == ROLE_TEACHER and doc.created_by_user != frappe.session.user:
		frappe.throw(_("This form is not yours."), frappe.PermissionError)

	used = frappe.db.count("MS Evaluation Entry", {"form": form})
	if used:
		# Deleting it would orphan the assessments made with it. Switching it
		# off keeps the history readable and takes it out of every list.
		frappe.db.set_value("MS Evaluation Form", form, "is_active", 0)
		frappe.db.commit()
		return {
			"success": True,
			"data": {"id": form, "deactivated": True},
			"message_en": "Form deactivated because it has been used.",
			"message_ar": f"النموذج مستخدم في {used} تقييماً، لذلك تم تعطيله بدل حذفه.",
		}

	frappe.delete_doc("MS Evaluation Form", form, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": form},
		"message_en": "Form deleted.",
		"message_ar": "تم حذف النموذج.",
	}


# ---------------------------------------------------------------------------
# The grid — a whole class against one form
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*STAFF)
def grid(form: str = None, student_group: str = None, course: str = None, persona: str = None):
	"""One class against one form: pupils down, criteria across.

	The same shape as mark entry, for the same reason — a teacher filling
	thirty pupils across fifteen lines will not open thirty dialogs.
	"""
	if not (form and student_group):
		return fail(
			message_en="A form and a class are required.",
			message_ar="يجب تحديد النموذج والشعبة.",
		)
	_assert_may_assess(persona, student_group)

	form_doc = frappe.get_doc("MS Evaluation Form", form)
	payload = _form_payload(form_doc)

	roster = frappe.get_all(
		"Student Group Student",
		filters={"parent": student_group, "active": 1},
		fields=["student", "student_name", "group_roll_number"],
		order_by="group_roll_number, student_name",
		limit_page_length=0,
	)
	if not roster:
		return {"form": payload, "students": [], "rows": {}}

	students = [r.student for r in roster]
	entries = frappe.get_all(
		"MS Evaluation Entry",
		filters={
			"form": form,
			"student": ["in", students],
			**({"course": course} if course else {}),
		},
		fields=["name", "student", "total_score", "max_score", "percent", "notes",
		        "is_published", "evaluated_on"],
		limit_page_length=0,
	)

	# Answers for the whole class in one query rather than one per pupil.
	answers: dict[str, dict[str, dict]] = {}
	if entries:
		by_entry = {e.name: e.student for e in entries}
		for row in frappe.get_all(
			"MS Evaluation Answer",
			filters={"parent": ["in", list(by_entry)], "parenttype": "MS Evaluation Entry"},
			fields=["parent", "criterion_key", "value_label", "score", "note"],
			limit_page_length=0,
		):
			student = by_entry.get(row.parent)
			if student:
				answers.setdefault(student, {})[row.criterion_key] = {
					"value": row.value_label,
					"score": flt(row.score),
					"note": row.note,
				}

	state = {
		e.student: {
			"entry": e.name,
			"total": flt(e.total_score),
			"max": flt(e.max_score),
			"percent": flt(e.percent),
			"notes": e.notes,
			"is_published": bool(cint(e.is_published)),
			"evaluated_on": str(e.evaluated_on or ""),
		}
		for e in entries
	}

	return {
		"form": payload,
		"student_group": student_group,
		"course": course,
		"students": [
			{
				"id": r.student,
				"name": r.student_name,
				"roll": cint(r.group_roll_number),
				**state.get(r.student, {"entry": None, "total": 0, "percent": 0, "is_published": False}),
			}
			for r in roster
		],
		"answers": answers,
	}


def _score_for(form_doc, criterion, value_label: str, raw_score) -> float:
	"""What one answer is worth.

	A مقياس answer takes the score its option carries; a numeric form takes
	what was typed, capped at the line's maximum so a slip cannot inflate a
	total past what the form allows.
	"""
	if form_doc.scale_type in ("مقياس", "نعم/لا"):
		for option in form_doc.scale or []:
			if option.label == value_label:
				return flt(option.score)
		return 0.0
	if form_doc.scale_type == "علامة رقمية":
		value = flt(raw_score)
		ceiling = flt(criterion.max_score)
		return min(value, ceiling) if ceiling else value
	return 0.0


def _write_entry(form_doc, student: str, student_group: str, course: str | None, row: dict):
	"""Create or update one pupil's assessment against a form."""
	by_key = {c.name: c for c in (form_doc.criteria or [])}
	values = row.get("values") or {}

	existing = frappe.get_all(
		"MS Evaluation Entry",
		filters={
			"form": form_doc.name,
			"student": student,
			**({"course": course} if course else {}),
		},
		pluck="name",
		limit=1,
	)
	doc = (
		frappe.get_doc("MS Evaluation Entry", existing[0])
		if existing
		else frappe.new_doc("MS Evaluation Entry")
	)

	doc.form = form_doc.name
	doc.form_title = form_doc.title
	doc.student = student
	doc.student_name = frappe.db.get_value("Student", student, "student_name")
	doc.student_group = student_group
	doc.course = course
	doc.evaluated_by = frappe.session.user
	doc.evaluated_on = now()
	if "notes" in row:
		doc.notes = row.get("notes")
	if "is_published" in row:
		doc.is_published = 1 if cint(row.get("is_published")) else 0

	total = 0.0
	maximum = 0.0
	doc.set("answers", [])
	for key, criterion in by_key.items():
		answer = values.get(key) or {}
		label = (answer.get("value") or "").strip() if isinstance(answer, dict) else str(answer)
		raw = answer.get("score") if isinstance(answer, dict) else None
		note = answer.get("note") if isinstance(answer, dict) else None
		if not label and raw in (None, ""):
			continue

		score = _score_for(form_doc, criterion, label, raw)
		total += score
		if form_doc.scale_type in ("مقياس", "نعم/لا"):
			maximum += max((flt(o.score) for o in form_doc.scale or []), default=0)
		else:
			maximum += flt(criterion.max_score)

		doc.append(
			"answers",
			{
				"criterion_key": key,
				# Copied, not linked: editing the form next term must not
				# rewrite what this assessment appears to have asked.
				"category": criterion.category,
				"item": criterion.item,
				"value_label": label or None,
				"score": score,
				"note": note,
			},
		)

	doc.total_score = total
	doc.max_score = maximum
	doc.percent = round(total * 100 / maximum, 2) if maximum else 0
	doc.save(ignore_permissions=True)
	return doc


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def save_grid(payload: str | dict = None, persona: str = None):
	"""Save a whole class's assessments in one go."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	form = data.get("form")
	student_group = data.get("student_group")
	rows = data.get("rows") or {}

	if not (form and student_group):
		return fail(
			message_en="A form and a class are required.",
			message_ar="يجب تحديد النموذج والشعبة.",
		)
	_assert_may_assess(persona, student_group)

	form_doc = frappe.get_doc("MS Evaluation Form", form)
	course = data.get("course")

	# Only pupils actually in the class: a forged id in the payload must not
	# create an assessment for someone else's pupil.
	roster = set(
		frappe.get_all(
			"Student Group Student",
			filters={"parent": student_group, "active": 1},
			pluck="student",
		)
	)

	saved = 0
	for student, row in rows.items():
		if student not in roster:
			continue
		_write_entry(form_doc, student, student_group, course, row or {})
		saved += 1

	frappe.db.commit()
	return {
		"success": True,
		"data": {"saved": saved},
		"message_en": f"Saved {saved} assessment(s).",
		"message_ar": f"تم حفظ تقييم {saved} طالباً.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def save_entry(payload: str | dict = None, persona: str = None):
	"""Assess one pupil, from their own page."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	form = data.get("form")
	student = data.get("student")
	if not (form and student):
		return fail(
			message_en="A form and a student are required.",
			message_ar="يجب تحديد النموذج والطالب.",
		)

	student_group = data.get("student_group") or frappe.db.get_value(
		"Student Group Student", {"student": student, "active": 1}, "parent"
	)
	_assert_may_assess(persona, student_group)

	form_doc = frappe.get_doc("MS Evaluation Form", form)
	doc = _write_entry(form_doc, student, student_group, data.get("course"), data)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "total": doc.total_score, "percent": doc.percent},
		"message_en": "Assessment saved.",
		"message_ar": "تم حفظ التقييم.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def publish_entries(payload: str | dict = None, persona: str = None):
	"""Show — or hide — a set of assessments from families."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	entries = data.get("entries") or []
	published = 1 if cint(data.get("is_published", 1)) else 0
	if not entries:
		return fail(message_en="Nothing selected.", message_ar="لم يتم تحديد أي تقييم.")

	rows = frappe.get_all(
		"MS Evaluation Entry",
		filters={"name": ["in", entries]},
		fields=["name", "student_group"],
		limit_page_length=0,
	)
	for row in rows:
		_assert_may_assess(persona, row.student_group)
		frappe.db.set_value("MS Evaluation Entry", row.name, "is_published", published)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"count": len(rows)},
		"message_en": "Visibility updated.",
		"message_ar": (
			f"تم إظهار {len(rows)} تقييماً للطلاب وأولياء الأمور."
			if published
			else f"تم إخفاء {len(rows)} تقييماً."
		),
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def student_evaluations(student: str = None, form_type: str = None, persona: str = None):
	"""One pupil's assessments across every form.

	A family sees only what a teacher chose to publish; an unpublished
	assessment is a working note, not a report.
	"""
	if not student:
		return fail(message_en="A student is required.", message_ar="يجب تحديد الطالب.")

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		mine = resolve_scope(persona).get("students") or []
		if student not in mine:
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	filters: dict = {"student": student}
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		filters["is_published"] = 1
	apply_period(filters, "MS Evaluation Entry")

	entries = frappe.get_all(
		"MS Evaluation Entry",
		filters=filters,
		fields=[
			"name", "form", "form_title", "course", "total_score", "max_score",
			"percent", "notes", "is_published", "evaluated_on", "student_group",
		],
		order_by="evaluated_on desc",
		limit_page_length=100,
	)
	if not entries:
		return {"entries": []}

	types = {
		r.name: r.form_type
		for r in frappe.get_all(
			"MS Evaluation Form",
			filters={"name": ["in", [e.form for e in entries]]},
			fields=["name", "form_type"],
		)
	}
	if form_type:
		entries = [e for e in entries if types.get(e.form) == form_type]
	if not entries:
		return {"entries": []}

	answers: dict[str, list] = {}
	for row in frappe.get_all(
		"MS Evaluation Answer",
		filters={"parent": ["in", [e.name for e in entries]], "parenttype": "MS Evaluation Entry"},
		fields=["parent", "category", "item", "value_label", "score", "note"],
		limit_page_length=0,
	):
		answers.setdefault(row.parent, []).append(
			{
				"category": row.category,
				"item": row.item,
				"value": row.value_label,
				"score": flt(row.score),
				"note": row.note,
			}
		)

	return {
		"entries": [
			{
				"id": e.name,
				"form": e.form,
				"form_title": e.form_title,
				"form_type": types.get(e.form),
				"course": e.course,
				"student_group": e.student_group,
				"total": flt(e.total_score),
				"max": flt(e.max_score),
				"percent": flt(e.percent),
				"notes": e.notes,
				"is_published": bool(cint(e.is_published)),
				"evaluated_on": str(e.evaluated_on or ""),
				"answers": answers.get(e.name, []),
			}
			for e in entries
		]
	}
