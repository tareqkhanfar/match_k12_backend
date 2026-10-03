# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""نماذج التقييم الدورية — the monthly (or termly, or yearly) report on a pupil.

The paper this replaces is one page per pupil: a row per subject with its
rating, a box the class teacher alone fills (cleanliness and conduct, their
remarks), the class teacher's signature and the principal's. So a form here
carries criteria of two kinds:

  * subject criteria — answered for every subject, by the teacher who
    teaches that subject in that section;
  * homeroom criteria — answered once per pupil, by the section's مربي الصف
    (`Student Group.ms_homeroom_instructor`) and nobody else.

A form is filled period by period ("شهر (2)"); the office opens one period at
a time for teachers and may itself correct any period. Every answer records
who gave it, because the office's view of a section is exactly that: which
teacher has rated which pupil, and who is still missing.
"""

import json
import re

import frappe
import jinja2
from frappe import _
from frappe.utils import cint, escape_html, flt, get_fullname, now

from match_schools.api import forms_print, print_designs
from match_schools.api.gradeflow import teaching_pairs
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	apply_period,
	fail,
	instructor_groups,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)
from match_schools.api.utils import get_default_academic_year

OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (*OFFICE, ROLE_TEACHER)

HOMEROOM = "homeroom"
SUBJECT = "subject"
SCOPES = (SUBJECT, HOMEROOM)
TYPES = ("scale", "text", "number")

# What a new form starts from; every list can be edited.
PERIOD_PRESETS = {
	"شهري": [f"شهر ({i})" for i in range(1, 11)],
	"كل شهرين": ["الفترة الأولى", "الفترة الثانية", "الفترة الثالثة", "الفترة الرابعة", "الفترة الخامسة"],
	"نصفي": ["الفصل الأول", "الفصل الثاني"],
	"سنوي": ["نهاية العام"],
}
HEADINGS = {
	"شهري": "التقييم الشهري للطالب",
	"كل شهرين": "التقييم الدوري للطالب",
	"نصفي": "التقييم النصفي للطالب",
	"سنوي": "التقييم السنوي للطالب",
}
SCALE_PRESET = [
	{"label": "ممتاز", "tone": "success"},
	{"label": "جيد جدا", "tone": "info"},
	{"label": "جيد", "tone": "primary"},
	{"label": "مقبول", "tone": "warning"},
	{"label": "ضعيف", "tone": "danger"},
]
CRITERIA_PRESET = [
	{"label": "التقييم", "scope": SUBJECT, "type": "scale"},
	{"label": "النظافة والسلوك", "scope": HOMEROOM, "type": "scale"},
	{"label": "ملاحظات مربي الصف", "scope": HOMEROOM, "type": "text"},
]
MAX_CRITERIA = 30
MAX_TEXT = 1000


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


def _json_list(raw) -> list:
	if isinstance(raw, list):
		return raw
	try:
		value = json.loads(raw or "[]")
	except (TypeError, ValueError):
		return []
	return value if isinstance(value, list) else []


def _form(doc) -> dict:
	criteria = [c for c in _json_list(doc.criteria) if isinstance(c, dict) and c.get("key")]
	return {
		"id": doc.name,
		"title": doc.title,
		"periodType": doc.period_type,
		"academicYear": doc.academic_year,
		"isActive": bool(cint(doc.is_active)),
		"openPeriod": doc.open_period or "",
		"heading": doc.heading or HEADINGS.get(doc.period_type, ""),
		"principalName": doc.principal_name or "",
		"printLogo": doc.print_logo or "",
		"periods": [str(p) for p in _json_list(doc.periods)],
		"programs": [str(p) for p in _json_list(doc.programs)],
		"scale": [s for s in _json_list(doc.scale) if isinstance(s, dict) and s.get("label")],
		"criteria": criteria,
		"notes": doc.notes or "",
	}


def _load(form: str | None):
	if not form or not frappe.db.exists("MS Periodic Form", form):
		frappe.throw(_("Form not found."), frappe.DoesNotExistError)
	return frappe.get_doc("MS Periodic Form", form)


def _applies(form: dict, program: str | None) -> bool:
	return not form["programs"] or (program or "") in form["programs"]


def _criteria(form: dict, scope: str) -> list[dict]:
	return [c for c in form["criteria"] if c.get("scope") == scope]


def _visible_forms(persona: str) -> list[dict]:
	"""Forms of the year chosen in the header (or of no particular year)."""
	year = get_default_academic_year()
	filters: dict = {}
	if persona not in OFFICE:
		filters["is_active"] = 1
	rows = frappe.get_all(
		"MS Periodic Form", filters=filters, pluck="name", order_by="creation desc", limit_page_length=0
	)
	forms = [_form(frappe.get_doc("MS Periodic Form", n)) for n in rows]
	return [f for f in forms if not f["academicYear"] or not year or f["academicYear"] == year]


# ---------------------------------------------------------------------------
# Who teaches what, and who is the class teacher
# ---------------------------------------------------------------------------


def _names(doctype: str, field: str, ids) -> dict[str, str]:
	ids = [i for i in set(ids) if i]
	if not ids:
		return {}
	return {
		r.name: r.get(field) or r.name
		for r in frappe.get_all(doctype, filters={"name": ["in", ids]}, fields=["name", field])
	}


def _program_order(program: str | None) -> dict[str, int]:
	if not program:
		return {}
	return {
		r.course: r.idx
		for r in frappe.get_all(
			"Program Course", filters={"parent": program, "parenttype": "Program"}, fields=["course", "idx"]
		)
	}


def _section_courses(group: str, program: str | None, extra=()) -> list[dict]:
	"""The subjects a section is taught, in the grade's order, with their teachers.

	The grade's subject list, plus anything the timetable (weekly pattern and
	dated lessons) teaches the section beyond it — so the report prints every
	subject even while the timetable is still being filled in. Teachers come
	from the timetable.
	"""
	teachers: dict[str, set[str]] = {}
	sources = [("Course Schedule", {"student_group": group, "docstatus": ["<", 2]})]
	if frappe.db.table_exists("MS Timetable Slot"):
		sources.insert(0, ("MS Timetable Slot", {"student_group": group, "active": 1}))
	for doctype, filters in sources:
		for r in frappe.get_all(
			doctype, filters=filters, fields=["course", "instructor"], distinct=True, limit_page_length=0
		):
			if r.course:
				teachers.setdefault(r.course, set())
				if r.instructor:
					teachers[r.course].add(r.instructor)

	order = _program_order(program)
	courses = set(teachers) | set(order)
	courses |= {c for c in extra if c}
	course_names = _names("Course", "course_name", courses)
	people = _names("Instructor", "instructor_name", {i for s in teachers.values() for i in s})
	return [
		{
			"id": c,
			"name": course_names.get(c, c),
			"teachers": sorted(
				({"id": i, "name": people.get(i, i)} for i in teachers.get(c, ())),
				key=lambda t: t["name"],
			),
		}
		for c in sorted(courses, key=lambda c: (order.get(c, 999), course_names.get(c, c)))
	]


def _group_info(group: str) -> dict:
	g = frappe.db.get_value(
		"Student Group",
		group,
		["name", "student_group_name", "program", "batch", "academic_year", "ms_homeroom_instructor"],
		as_dict=True,
	)
	if not g:
		frappe.throw(_("Section not found."), frappe.DoesNotExistError)
	homeroom = g.ms_homeroom_instructor
	return {
		"id": g.name,
		"name": g.student_group_name or g.name,
		"program": g.program,
		"batch": g.batch,
		"academicYear": g.academic_year,
		"homeroom": (
			{"id": homeroom, "name": frappe.db.get_value("Instructor", homeroom, "instructor_name") or homeroom}
			if homeroom
			else None
		),
	}


def _teacher(persona: str) -> tuple[str | None, dict[str, list[str]], set[str]]:
	"""(instructor, section -> subjects they teach there, sections they are مربي of)."""
	instructor = resolve_scope(persona).get("instructor")
	if not instructor:
		return None, {}, set()
	live = set(instructor_groups(instructor))
	pairs = {g: c for g, c in teaching_pairs(instructor).items() if g in live}
	filters: dict = {"ms_homeroom_instructor": instructor, "disabled": 0}
	apply_period(filters, "Student Group")
	homeroom = set(frappe.get_all("Student Group", filters=filters, pluck="name", limit_page_length=0))
	return instructor, pairs, homeroom


def _roster(group: str) -> list[dict]:
	return [
		{"id": r.student, "name": r.student_name, "roll": cint(r.group_roll_number) or None}
		for r in frappe.get_all(
			"Student Group Student",
			filters={"parent": group, "parenttype": "Student Group", "active": 1},
			fields=["student", "student_name", "group_roll_number"],
			order_by="group_roll_number, student_name",
			limit_page_length=0,
		)
	]


def _assert_may_fill(persona: str, form: dict, group: dict, scope: str, course: str | None):
	"""A teacher fills only their own subject in their own section, and the
	homeroom lines only as that section's مربي الصف."""
	if persona in OFFICE:
		return
	instructor, pairs, homeroom = _teacher(persona)
	if scope == HOMEROOM:
		if group["id"] not in homeroom:
			frappe.throw(
				_("Only the section's class teacher fills these lines."), frappe.PermissionError
			)
	elif course not in pairs.get(group["id"], []):
		frappe.throw(_("You only evaluate the subjects you teach in this section."), frappe.PermissionError)


def _editable(persona: str, form: dict, period: str) -> tuple[bool, str]:
	if persona in OFFICE:
		return True, ""
	if not form["isActive"]:
		return False, "النموذج موقوف."
	if not form["openPeriod"]:
		return False, "الإدخال مغلق حالياً — تفتحه الإدارة."
	if period != form["openPeriod"]:
		return False, f"الإدخال مفتوح لـ«{form['openPeriod']}» فقط."
	return True, ""


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*STAFF)
def options(persona: str = None):
	"""Forms, and the sections (with subjects) the caller may work in."""
	forms = _visible_forms(persona)
	sections = []
	if persona in OFFICE:
		filters: dict = {"disabled": 0}
		apply_period(filters, "Student Group")
		groups = frappe.get_all(
			"Student Group",
			filters=filters,
			fields=["name", "student_group_name", "program", "ms_homeroom_instructor"],
			order_by="program, student_group_name",
			limit_page_length=0,
		)
		people = _names("Instructor", "instructor_name", [g.ms_homeroom_instructor for g in groups])
		for g in groups:
			sections.append(
				{
					"id": g.name,
					"name": g.student_group_name or g.name,
					"program": g.program,
					"homeroom": (
						{"id": g.ms_homeroom_instructor, "name": people.get(g.ms_homeroom_instructor)}
						if g.ms_homeroom_instructor
						else None
					),
					"isHomeroom": True,
					"courses": [
						{"id": c["id"], "name": c["name"], "teachers": c["teachers"]}
						for c in _section_courses(g.name, g.program)
					],
				}
			)
	else:
		instructor, pairs, homeroom = _teacher(persona)
		ids = sorted(set(pairs) | homeroom)
		groups = frappe.get_all(
			"Student Group",
			filters={"name": ["in", ids or [""]]},
			fields=["name", "student_group_name", "program"],
			order_by="program, student_group_name",
		)
		course_names = _names("Course", "course_name", {c for cs in pairs.values() for c in cs})
		for g in groups:
			order = _program_order(g.program)
			mine = sorted(pairs.get(g.name, []), key=lambda c: (order.get(c, 999), course_names.get(c, c)))
			sections.append(
				{
					"id": g.name,
					"name": g.student_group_name or g.name,
					"program": g.program,
					"homeroom": None,
					"isHomeroom": g.name in homeroom,
					"courses": [{"id": c, "name": course_names.get(c, c), "teachers": []} for c in mine],
				}
			)

	return {
		"forms": forms,
		"sections": sections,
		"canManage": persona in OFFICE,
		"presets": {
			"periods": PERIOD_PRESETS,
			"headings": HEADINGS,
			"scale": SCALE_PRESET,
			"criteria": CRITERIA_PRESET,
		},
		"programs": frappe.get_all("Program", pluck="name", order_by="name") if persona in OFFICE else [],
		"academicYear": get_default_academic_year(),
	}


@frappe.whitelist()
@ms_endpoint(*STAFF)
def sheet(
	form: str = None,
	period: str = None,
	student_group: str = None,
	scope: str = SUBJECT,
	course: str = None,
	persona: str = None,
):
	"""A section against one form for one period: pupils down, criteria across.

	`scope` subject: one subject's lines, for its teacher. `scope` homeroom:
	the class teacher's lines.
	"""
	if not (form and period and student_group):
		return fail(message_en="Form, period and section are required.", message_ar="اختر النموذج والفترة والشعبة.")
	if scope not in SCOPES or (scope == SUBJECT and not course):
		return fail(message_en="Choose a subject.", message_ar="اختر المادة.")

	f = _form(_load(form))
	if period not in f["periods"]:
		return fail(message_en="Unknown period.", message_ar="الفترة غير موجودة في النموذج.")
	group = _group_info(student_group)
	if not _applies(f, group["program"]):
		return fail(message_en="This form is not for this grade.", message_ar="هذا النموذج ليس لهذا الصف.")
	_assert_may_fill(persona, f, group, scope, course)

	criteria = _criteria(f, scope)
	students = _roster(student_group)
	course_key = course if scope == SUBJECT else None
	values, by = {}, {}
	for e in frappe.get_all(
		"MS Periodic Entry",
		filters={
			"form": form,
			"period": period,
			"student": ["in", [s["id"] for s in students] or [""]],
			"scope": scope,
			**({"course": course_key} if course_key else {}),
		},
		fields=["student", "answers", "evaluator_name", "evaluated_on"],
		limit_page_length=0,
	):
		values[e.student] = frappe.parse_json(e.answers or "{}") or {}
		by[e.student] = {"name": e.evaluator_name, "on": str(e.evaluated_on or "")[:16]}

	editable, reason = _editable(persona, f, period)
	return {
		"form": f,
		"period": period,
		"section": group,
		"scope": scope,
		"course": course_key,
		"courseName": frappe.db.get_value("Course", course_key, "course_name") if course_key else "",
		"criteria": criteria,
		"students": students,
		"values": values,
		"by": by,
		"editable": editable,
		"reason": reason,
	}


def _clean(criterion: dict, raw, scale: set[str]):
	"""One answer as it will be stored, or raise with what is wrong with it."""
	if raw is None:
		return None
	text = str(raw).strip()
	if not text:
		return None
	kind = criterion.get("type")
	if kind == "scale":
		if text not in scale:
			raise ValueError(f"«{text}» ليس من خيارات المقياس.")
		return text
	if kind == "number":
		try:
			number = float(text)
		except ValueError:
			raise ValueError(f"«{criterion.get('label')}» يقبل رقماً فقط.")
		ceiling = flt(criterion.get("max"))
		if number < 0 or (ceiling and number > ceiling):
			raise ValueError(f"«{criterion.get('label')}» بين 0 و{ceiling:g}.")
		return number
	return text[:MAX_TEXT]


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def save_sheet(payload: str | dict = None, persona: str = None):
	"""Save a section's answers for one subject (or the homeroom lines).

	A pupil sent with nothing filled has their entry removed, so clearing a
	row in the grid clears it here too.
	"""
	data = parse_json_arg(payload) or {}
	form, period, student_group = data.get("form"), data.get("period"), data.get("student_group")
	scope = data.get("scope") or SUBJECT
	course = data.get("course") if scope == SUBJECT else None
	rows = data.get("rows") or {}
	if not (form and period and student_group) or scope not in SCOPES or (scope == SUBJECT and not course):
		return fail(message_en="Form, period, section and subject are required.", message_ar="اختر النموذج والفترة والشعبة والمادة.")

	doc = _load(form)
	f = _form(doc)
	if period not in f["periods"]:
		return fail(message_en="Unknown period.", message_ar="الفترة غير موجودة في النموذج.")
	group = _group_info(student_group)
	if not _applies(f, group["program"]):
		return fail(message_en="This form is not for this grade.", message_ar="هذا النموذج ليس لهذا الصف.")
	_assert_may_fill(persona, f, group, scope, course)
	editable, reason = _editable(persona, f, period)
	if not editable:
		return fail(message_en="Entry is closed.", message_ar=reason)

	criteria = _criteria(f, scope)
	scale = {s["label"] for s in f["scale"]}
	roster = {s["id"]: s["name"] for s in _roster(student_group)}
	instructor = resolve_scope(persona).get("instructor") if persona == ROLE_TEACHER else None
	evaluator = get_fullname(frappe.session.user)

	# Every row is checked before any is written: a bad answer on the tenth
	# pupil must not leave the first nine saved behind the teacher's back.
	checked: dict[str, dict] = {}
	for student, answers in rows.items():
		if student not in roster or not isinstance(answers, dict):
			continue
		clean = {}
		for c in criteria:
			try:
				value = _clean(c, answers.get(c["key"]), scale)
			except ValueError as err:
				return fail(message_en=str(err), message_ar=f"{roster[student]}: {err}")
			if value is not None:
				clean[c["key"]] = value
		checked[student] = clean

	saved = cleared = 0
	for student, clean in checked.items():
		key = f"{form}|{period}|{student}|{course or HOMEROOM}"
		existing = frappe.db.get_value("MS Periodic Entry", {"entry_key": key}, "name")
		if not clean:
			if existing:
				frappe.delete_doc("MS Periodic Entry", existing, ignore_permissions=True, force=True)
				cleared += 1
			continue

		saved += 1
		entry = frappe.get_doc("MS Periodic Entry", existing) if existing else frappe.new_doc("MS Periodic Entry")
		# Unchanged rows keep the teacher who actually gave the rating.
		if existing and (frappe.parse_json(entry.answers or "{}") or {}) == clean:
			continue
		entry.update(
			{
				"entry_key": key,
				"form": form,
				"period": period,
				"academic_year": doc.academic_year or group["academicYear"],
				"scope": scope,
				"student": student,
				"student_name": roster[student],
				"student_group": student_group,
				"program": group["program"],
				"course": course,
				"answers": json.dumps(clean, ensure_ascii=False),
				"instructor": instructor,
				"evaluated_by": frappe.session.user,
				"evaluator_name": evaluator,
				"evaluated_on": now(),
			}
		)
		entry.save(ignore_permissions=True)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"saved": saved, "cleared": cleared},
		"message_en": f"Saved {saved}.",
		"message_ar": f"تم حفظ تقييم {saved} طالباً" + (f"، ومسح {cleared}" if cleared else "") + ".",
	}


def _section_entries(form: str, period: str, students: list[str]) -> dict[str, dict]:
	"""student -> course (or "homeroom") -> {values, by, on}."""
	out: dict[str, dict] = {}
	for e in frappe.get_all(
		"MS Periodic Entry",
		filters={"form": form, "period": period, "student": ["in", students or [""]]},
		fields=["student", "scope", "course", "answers", "evaluator_name", "evaluated_on"],
		limit_page_length=0,
	):
		out.setdefault(e.student, {})[e.course if e.scope == SUBJECT else HOMEROOM] = {
			"values": frappe.parse_json(e.answers or "{}") or {},
			"by": e.evaluator_name or "",
			"on": str(e.evaluated_on or "")[:16],
		}
	return out


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def overview(form: str = None, period: str = None, student_group: str = None, persona: str = None):
	"""Everything a section's teachers have entered for one period — and what
	is still missing, subject by subject."""
	if not (form and period and student_group):
		return fail(message_en="Form, period and section are required.", message_ar="اختر النموذج والفترة والشعبة.")
	f = _form(_load(form))
	group = _group_info(student_group)
	students = _roster(student_group)
	entries = _section_entries(form, period, [s["id"] for s in students])
	used = {c for per in entries.values() for c in per if c != HOMEROOM}
	return {
		"form": f,
		"period": period,
		"section": group,
		"applies": _applies(f, group["program"]),
		"courses": _section_courses(student_group, group["program"], used),
		"students": students,
		"entries": entries,
	}


# ---------------------------------------------------------------------------
# Forms (office)
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def list_forms(persona: str = None):
	counts = {
		r.form: r.n
		for r in frappe.db.sql(
			"select form, count(*) as n from `tabMS Periodic Entry` group by form", as_dict=True
		)
	}
	return {
		"forms": [
			{**_form(frappe.get_doc("MS Periodic Form", n)), "entries": counts.get(n, 0)}
			for n in frappe.get_all("MS Periodic Form", pluck="name", order_by="creation desc", limit_page_length=0)
		]
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def save_form(payload: str | dict = None, persona: str = None):
	"""Create or edit a form, whole."""
	data = parse_json_arg(payload) or {}
	title = (data.get("title") or "").strip()
	if not title:
		return fail(message_en="A name is required.", message_ar="اسم النموذج مطلوب.")
	period_type = data.get("periodType") or "شهري"
	if period_type not in PERIOD_PRESETS:
		return fail(message_en="Unknown period type.", message_ar="نوع الدورية غير معروف.")

	periods = []
	for p in data.get("periods") or PERIOD_PRESETS[period_type]:
		p = str(p or "").strip()[:60]
		if p and p not in periods:
			periods.append(p)
	if not periods:
		return fail(message_en="Add the periods.", message_ar="أضف فترة واحدة على الأقل.")
	open_period = (data.get("openPeriod") or "").strip()
	if open_period and open_period not in periods:
		return fail(message_en="The open period is not in the list.", message_ar="الفترة المفتوحة ليست من فترات النموذج.")

	scale = []
	for s in data.get("scale") or []:
		label = str((s or {}).get("label") or "").strip()[:40]
		if label and label not in [x["label"] for x in scale]:
			scale.append({"label": label, "tone": (s or {}).get("tone") or "muted"})

	criteria = []
	for c in (data.get("criteria") or [])[: MAX_CRITERIA + 1]:
		c = c or {}
		label = str(c.get("label") or "").strip()[:120]
		if not label:
			continue
		kind = c.get("type") if c.get("type") in TYPES else "scale"
		key = str(c.get("key") or "").strip()
		if not re.fullmatch(r"[a-z0-9]{4,16}", key) or key in [x["key"] for x in criteria]:
			key = frappe.generate_hash(length=8)
		criteria.append(
			{
				"key": key,
				"label": label,
				"scope": c.get("scope") if c.get("scope") in SCOPES else SUBJECT,
				"type": kind,
				**({"max": flt(c.get("max"))} if kind == "number" and flt(c.get("max")) else {}),
			}
		)
	if not criteria:
		return fail(message_en="Add at least one criterion.", message_ar="أضف معياراً واحداً على الأقل.")
	if len(criteria) > MAX_CRITERIA:
		return fail(message_en="Too many criteria.", message_ar=f"الحد الأقصى {MAX_CRITERIA} معياراً.")
	if any(c["type"] == "scale" for c in criteria) and not scale:
		return fail(message_en="The scale needs its options.", message_ar="حدّد خيارات المقياس (ممتاز، جيد جدا…).")

	programs = [p for p in (data.get("programs") or []) if frappe.db.exists("Program", p)]
	year = data.get("academicYear")
	if year and not frappe.db.exists("Academic Year", year):
		year = None

	name = data.get("id")
	doc = frappe.get_doc("MS Periodic Form", name) if name else frappe.new_doc("MS Periodic Form")
	if not name:
		doc.created_by_user = frappe.session.user
	doc.update(
		{
			"title": title[:140],
			"period_type": period_type,
			"academic_year": year,
			"is_active": 1 if cint(data.get("isActive", 1)) else 0,
			"open_period": open_period or None,
			"heading": (data.get("heading") or "").strip()[:140] or HEADINGS[period_type],
			"principal_name": (data.get("principalName") or "").strip()[:140],
			"print_logo": data.get("printLogo") or None,
			"periods": json.dumps(periods, ensure_ascii=False),
			"programs": json.dumps(programs, ensure_ascii=False),
			"scale": json.dumps(scale, ensure_ascii=False),
			"criteria": json.dumps(criteria, ensure_ascii=False),
			"notes": data.get("notes") or "",
		}
	)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"success": True, "data": _form(doc), "message_en": "Saved.", "message_ar": "تم حفظ النموذج."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def set_open_period(form: str = None, period: str = None, persona: str = None):
	"""Open one period for teachers — or close entry (empty period)."""
	doc = _load(form)
	period = (period or "").strip()
	if period and period not in _form(doc)["periods"]:
		return fail(message_en="Unknown period.", message_ar="الفترة غير موجودة في النموذج.")
	doc.db_set("open_period", period or None)
	frappe.db.commit()
	return {
		"success": True,
		"data": _form(doc),
		"message_en": "Updated.",
		"message_ar": f"فُتح الإدخال لـ«{period}»." if period else "أُغلق الإدخال للمعلمين.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def delete_form(form: str = None, persona: str = None):
	"""Remove an unused form; a used one is switched off, keeping its history."""
	doc = _load(form)
	used = frappe.db.count("MS Periodic Entry", {"form": doc.name})
	if used:
		doc.db_set("is_active", 0)
		frappe.db.commit()
		return {
			"success": True,
			"data": {"id": doc.name, "deactivated": True},
			"message_en": "Deactivated.",
			"message_ar": f"النموذج فيه {used} تقييماً، لذلك أُوقف بدل حذفه.",
		}
	frappe.delete_doc("MS Periodic Form", doc.name, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {"success": True, "data": {"id": form}, "message_en": "Deleted.", "message_ar": "تم حذف النموذج."}


# ---------------------------------------------------------------------------
# The printed report — one page per pupil, as the school's paper
# ---------------------------------------------------------------------------

# The report is a print design (Settings › تصاميم الطباعة, «تقرير التقييم
# الدوري»): this is the one it ships with — the school's paper, drawn for one
# pupil and repeated a page each. The values it is given are already escaped.

CARD_CSS = """
  .pc-page + .pc-page { page-break-before: always; break-before: page; }
  .pc-page { font-size: 16px; line-height: 1.7; }
  .pc-head { text-align: center; font-weight: 800; font-size: 20px; line-height: 1.5; margin: 1mm 0 6mm; }
  .pc-t { width: 100%; border-collapse: collapse; }
  .pc-t td, .pc-t th { border: 1px solid #1f2433; padding: 5px 10px; vertical-align: middle; }
  .pc-id td { height: 16mm; font-size: 18px; }
  .pc-id td b { font-weight: 700; }
  .pc-grid { margin-top: 0; }
  .pc-grid th { background: #d9d9d9; font-weight: 500; font-size: 18px; text-align: center; height: 11mm; }
  .pc-grid td { height: 9mm; }
  .pc-grid td.sub { width: 34%; }
  .pc-grid td.val { text-align: center; }
  .pc-opt { white-space: nowrap; }
  .pc-opt .on { font-weight: 800; border: 1.6px solid #1f2433; border-radius: 999px; padding: 0 7px; }
  .pc-home { margin-top: 7mm; }
  .pc-home td { height: 16mm; vertical-align: top; }
  .pc-home .lb { color: #333; }
  .pc-home .ans { font-weight: 700; }
  .pc-sign { margin-top: 9mm; font-size: 17px; }
  .pc-seal { text-align: center; margin-top: 8mm; font-weight: 700; }
  .pc-principal { margin-top: 2mm; font-weight: 700; line-height: 1.5; width: 60mm; margin-inline-start: auto; text-align: center; }
  .pc-dots { display: inline-block; min-width: 45mm; border-bottom: 1px dotted #555; }
"""

CARD_DESIGN = """{{ banner() }}
<div class="pc-head">
  <div>{{ heading_line }}</div>
  <div>للعام الدراسي {{ year_label }}</div>
</div>
<table class="pc-t pc-id"><tr>
  <td style="width:50%">الاسم: <b>{{ student.name }}</b></td>
  <td>الصف: <b>{{ grade }}</b></td>
  <td>الشعبة: <b>{{ section }}</b></td>
</tr></table>
{% if subject_criteria %}
<table class="pc-t pc-grid">
  <thead><tr><th>المادة</th>{% for c in subject_criteria %}<th>{{ c.label }}</th>{% endfor %}</tr></thead>
  <tbody>
  {% for s in subjects %}
    <tr><td class="sub">{{ s.name }}</td>{% for c in subject_criteria %}<td class="val">{{ answer(c, s.answers.get(c.key)) }}</td>{% endfor %}</tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}
{% if homeroom_criteria %}
<table class="pc-t pc-home"><tr>
  {% for c in homeroom_criteria %}<td><span class="lb">{{ c.label }} :</span> {{ answer(c, homeroom.get(c.key), false) }}</td>{% endfor %}
</tr></table>
{% endif %}
<div class="pc-sign">توقيع مربي الصف: {{ homeroom_teacher or '<span class="pc-dots"></span>' }}</div>
<div class="pc-seal">الخاتم</div>
<div class="pc-principal">مدير المدرسة<br>{{ principal }}</div>
"""

PRINT_KEY = "periodic_report"
PRINT_DEFAULT = {"design": CARD_DESIGN, "css": CARD_CSS, "theme": "letter", "orientation": "Portrait"}


def _year_label(year: str | None) -> str:
	found = re.findall(r"(\d{4})", year or "")
	if len(found) >= 2:
		return f"({found[0]}م – {found[1]}م)"
	return f"({found[0]}م)" if found else ""


def _heading_line(heading: str, period: str) -> str:
	# «للطالب» + «الفصل الأول» reads «للفصل الأول», not «لالفصل الأول».
	if period.startswith("ال"):
		return f"{heading} لل{period[2:]}"
	return f"{heading} ل{period}"


def _short_program(program: str | None) -> str:
	return re.sub(r"^\s*الصف\s+", "", program or "")


def _answer_html(criterion: dict, value, scale: list[str], circled: bool) -> str:
	if isinstance(value, jinja2.Undefined):
		value = None
	if criterion.get("type") == "scale" and circled:
		return (
			'<span class="pc-opt">'
			+ " / ".join(
				f'<span class="on">{escape_html(o)}</span>' if o == value else escape_html(o) for o in scale
			)
			+ "</span>"
		)
	if value in (None, ""):
		return '<span class="pc-dots"></span>'
	if isinstance(value, float) and value.is_integer():
		value = int(value)
	return f'<span class="ans">{escape_html(str(value))}</span>'


def _card_context(f: dict, period: str, group: dict, student: dict, courses: list[dict], entries: dict) -> dict:
	"""What the design is given for one pupil — text already escaped."""
	scale = [s["label"] for s in f["scale"]]
	mine = entries.get(student["id"], {})
	teacher = (group["homeroom"] or {}).get("name") or (mine.get(HOMEROOM) or {}).get("by") or ""

	def criteria(scope: str) -> list[dict]:
		return [{**c, "label": escape_html(c["label"])} for c in _criteria(f, scope)]

	return {
		"heading": escape_html(f["heading"]),
		"period": escape_html(period),
		"heading_line": escape_html(_heading_line(f["heading"], period)),
		"year_label": escape_html(_year_label(f["academicYear"] or group["academicYear"])),
		"form_title": escape_html(f["title"]),
		"student": {"id": student["id"], "name": escape_html(student["name"] or ""), "roll": student.get("roll")},
		"grade": escape_html(_short_program(group["program"])),
		"section": escape_html(group["batch"] or group["name"]),
		"section_name": escape_html(group["name"]),
		"subject_criteria": criteria(SUBJECT),
		"homeroom_criteria": criteria(HOMEROOM),
		"subjects": [
			{
				"id": c["id"],
				"name": escape_html(c["name"]),
				"answers": (mine.get(c["id"]) or {}).get("values") or {},
				"by": escape_html((mine.get(c["id"]) or {}).get("by") or ""),
			}
			for c in courses
		],
		"homeroom": (mine.get(HOMEROOM) or {}).get("values") or {},
		"homeroom_teacher": escape_html(teacher),
		"principal": escape_html(f["principalName"]),
		"scale": [escape_html(o) for o in scale],
		"answer": lambda c, value=None, circled=True: _answer_html(c, value, scale, circled),
	}


def _design(f: dict, design=None):
	"""The report's design — a draft being previewed, or the one it prints
	with now. A form given a letterhead of its own prints with that one."""
	design = frappe._dict(design or print_designs.effective(PRINT_KEY))
	if f.get("printLogo"):
		design.letterhead = f["printLogo"]
	return design


def render_cards(design, f: dict, period: str, group: dict, roster: list[dict], courses: list[dict], entries: dict) -> str:
	"""The report of every pupil in `roster`, a page each, in one document."""
	compiled = forms_print.compile_design(design.design)
	base = print_designs.base_context(design, fallback_class="pc-head")
	pages = [
		forms_print.draw(compiled, {**base, **_card_context(f, period, group, s, courses, entries)}) for s in roster
	]
	return forms_print.frame(pages, design.theme, design.orientation, design.css, "pc-page")


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def print_cards(
	form: str = None,
	period: str = None,
	student_group: str = None,
	students: str | list = None,
	persona: str = None,
):
	"""The report of each pupil chosen (all of the section when none are), as
	one printable document — a page each."""
	if not (form and period and student_group):
		return fail(message_en="Form, period and section are required.", message_ar="اختر النموذج والفترة والشعبة.")
	f = _form(_load(form))
	group = _group_info(student_group)
	roster = _roster(student_group)
	wanted = parse_json_arg(students) or []
	if wanted:
		roster = [s for s in roster if s["id"] in set(wanted)]
	if not roster:
		return fail(message_en="No students.", message_ar="لا يوجد طلاب للطباعة.")

	entries = _section_entries(form, period, [s["id"] for s in roster])
	used = {c for per in entries.values() for c in per if c != HOMEROOM}
	courses = _section_courses(student_group, group["program"], used)
	try:
		html = render_cards(_design(f), f, period, group, roster, courses, entries)
	except forms_print.DesignError as exc:
		return fail(
			message_en="The print design could not be rendered.",
			message_ar=f"{exc} — راجع «تقرير التقييم الدوري» في الإعدادات › تصاميم الطباعة.",
		)
	title = f"{f['title']} — {period} — {group['name']}"
	if len(roster) == 1:
		title = f"{f['title']} — {period} — {roster[0]['name']}"
	return {"html": html, "title": title, "count": len(roster)}


def _sample_report() -> tuple:
	"""A made-up pupil's report, for the editor on a site with none entered yet."""
	f = {
		"title": "التقييم الشهري",
		"heading": HEADINGS["شهري"],
		"academicYear": get_default_academic_year() or "2026-2027",
		"principalName": "",
		"printLogo": "",
		"scale": SCALE_PRESET,
		"criteria": [
			{"key": "rating", "label": "التقييم", "scope": SUBJECT, "type": "scale"},
			{"key": "conduct", "label": "النظافة والسلوك", "scope": HOMEROOM, "type": "scale"},
			{"key": "remarks", "label": "ملاحظات مربي الصف", "scope": HOMEROOM, "type": "text"},
		],
	}
	group = {
		"id": "sample", "name": "السابع أ", "program": "الصف السابع", "batch": "أ",
		"academicYear": f["academicYear"], "homeroom": {"id": "", "name": "أ. سامر يوسف"},
	}
	roster = [{"id": "sample-student", "name": "أحمد محمد علي الخطيب", "roll": 1}]
	names = ["التربية الإسلامية", "اللغة العربية", "اللغة الإنجليزية", "الرياضيات", "العلوم والحياة", "الدراسات الاجتماعية"]
	ratings = ["ممتاز", "جيد جدا", "ممتاز", "جيد", "جيد جدا", "ممتاز"]
	courses = [{"id": f"c{i}", "name": n, "teachers": []} for i, n in enumerate(names)]
	mine = {f"c{i}": {"values": {"rating": r}, "by": "", "on": ""} for i, r in enumerate(ratings)}
	mine[HOMEROOM] = {"values": {"conduct": "ممتاز", "remarks": "طالب مجتهد ومتعاون"}, "by": "", "on": ""}
	return f, "شهر (2)", group, roster, courses, {"sample-student": mine}


def print_sample(design) -> str:
	"""One pupil's report drawn with `design`: the latest one entered, or a
	made-up one when nothing has been entered yet."""
	last = frappe.get_all(
		"MS Periodic Entry",
		fields=["form", "period", "student_group", "student"],
		order_by="modified desc",
		limit=1,
	)
	if last and frappe.db.exists("MS Periodic Form", last[0].form) and frappe.db.exists(
		"Student Group", last[0].student_group
	):
		e = last[0]
		f = _form(_load(e.form))
		group = _group_info(e.student_group)
		roster = [s for s in _roster(e.student_group) if s["id"] == e.student][:1]
		if roster:
			entries = _section_entries(e.form, e.period, [roster[0]["id"]])
			used = {c for per in entries.values() for c in per if c != HOMEROOM}
			courses = _section_courses(e.student_group, group["program"], used)
			return render_cards(_design(f, design), f, e.period, group, roster, courses, entries)
	f, period, group, roster, courses, entries = _sample_report()
	return render_cards(_design(f, design), f, period, group, roster, courses, entries)
