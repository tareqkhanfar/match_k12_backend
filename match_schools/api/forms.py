# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The specialist forms a school keeps on a child outside the gradebook.

Nursing, counselling, special needs, learning difficulties, speech and
language: five files, each its own screen, each with its own forms. They share
one engine because the differences are in the questions, not in the mechanics
— and a school that needs a sixth file should get it by adding a category, not
by another copy of this code.

A form is designed once (fields, their order, their layout) and filled many
times, once per student. Behaviour evaluation (`MS Evaluation Form`) is a
different thing and is left alone: it scores a pupil against criteria, while
these record what a specialist observed.
"""

import json
import re

import frappe
from frappe.utils import cint, escape_html, flt, now_datetime, nowdate

from match_schools.api import forms_print
from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_TEACHER,
	apply_period,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
	resolve_scope,
)

CATEGORIES = {
	"nursing": "التمريض",
	"counselling": "الإرشاد",
	"special_needs": "الاحتياجات الخاصة",
	"learning_difficulties": "صعوبات التعلم",
	"speech_language": "النطق واللغة",
	# The school's own forms — the ones that are nobody's specialism:
	# permission slips, meeting minutes, duty rosters, requests.
	"school": "المدرسة",
}

# What a field can be. `Section` and `Heading` carry no value; they shape the
# form. Everything else is answered by whoever fills it in.
FIELD_TYPES = {
	"Section": "قسم",
	"Heading": "عنوان",
	"Data": "نص قصير",
	"Long Text": "نص طويل",
	"Number": "رقم",
	"Date": "تاريخ",
	"Time": "وقت",
	"Datetime": "تاريخ ووقت",
	"Select": "قائمة اختيار",
	"Multi Select": "اختيار متعدد",
	"Checkbox": "مربع اختيار",
	"Rating": "تقييم",
	"Table": "جدول",
	"Attach": "مرفق",
	"Student Table": "جدول طلاب",
	"Text Block": "نص ثابت",
}
LAYOUT_TYPES = ("Section", "Heading")
# Fields nobody answers: headings shape the form, a text block is printed as
# written.
NO_VALUE = LAYOUT_TYPES + ("Text Block",)


def _teachers_key(category: str) -> str:
	return f"ms_forms_teachers_may_fill:{category}"


def _teachers_may_fill(category: str) -> bool:
	"""Whether teachers may file forms in this file.

	Set per file, because the answer differs: a school may want every teacher
	writing counselling notes while the nursing record stays with the clinic.
	Allowed unless the administration says otherwise.
	"""
	stored = frappe.db.get_default(_teachers_key(category))
	return True if stored in (None, "") else bool(cint(stored))


def _design_key(category: str) -> str:
	return f"ms_forms_teachers_may_design:{category}"


def _teachers_may_design(category: str) -> bool:
	"""Whether teachers may add forms to this file and edit its forms.

	Off unless the administration turns it on: a form's design decides what
	every colleague records, so it is the office's until it says otherwise.
	Deleting a form stays with the office either way.
	"""
	return bool(cint(frappe.db.get_default(_design_key(category)) or 0))


def _assert_may_design(persona: str, category: str):
	if persona in BACK_OFFICE:
		return
	if persona == ROLE_TEACHER and category in CATEGORIES and _teachers_may_design(category):
		return
	frappe.throw(
		"تصميم نماذج هذا القسم مقصور على الإدارة. يمكن للإدارة السماح للمعلمين من إعدادات القسم.",
		frappe.PermissionError,
	)


def _category(category: str) -> str:
	if category not in CATEGORIES:
		frappe.throw(frappe._("Unknown form category."), frappe.ValidationError)
	return category


def _field_rows(doc) -> list[dict]:
	return [
		{
			"fieldname": f.fieldname,
			"label": f.label,
			"fieldtype": f.fieldtype,
			"options": f.options or "",
			"default": f.default_value or "",
			"reqd": cint(f.reqd),
			"width": f.width or "half",
			"description": f.description or "",
			"icon": f.get("icon") or "",
			"tone": f.get("tone") or "",
			"print_rows": cint(f.get("print_rows")),
			"idx": f.idx,
		}
		for f in sorted(doc.fields_table, key=lambda r: r.idx)
	]


def _template_payload(doc) -> dict:
	return {
		"name": doc.name,
		"title": doc.title,
		"category": doc.category,
		"categoryLabel": CATEGORIES.get(doc.category, doc.category),
		"description": doc.description or "",
		"isActive": cint(doc.is_active),
		"printTemplate": doc.print_template or "",
		"printCss": doc.get("print_css") or "",
		"entryFor": doc.get("entry_for") or "Student",
		"printTheme": doc.get("print_theme") or "soft",
		"printOrientation": doc.get("print_orientation") or "Portrait",
		"printLogo": doc.get("print_logo") or "",
		"printSchool": doc.get("print_school") or "",
		"printDepartment": doc.get("print_department") or "",
		"printMotto": doc.get("print_motto") or "",
		"printSignatures": doc.get("print_signatures") or "",
		"printFooter": doc.get("print_footer") or "",
		"subjectsCount": len(doc.get("subjects_table") or []),
		"fields": _field_rows(doc),
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def categories(persona: str = None):
	"""The five files, with how many forms each holds."""
	counts = {}
	for row in frappe.get_all(
		"MS Form Template", filters={"is_active": 1}, fields=["category"], limit_page_length=0
	):
		counts[row.category] = counts.get(row.category, 0) + 1
	return {
		"categories": [
			{
				"key": key,
				"label": label,
				"forms": counts.get(key, 0),
				"teachersMayFill": _teachers_may_fill(key),
				"teachersMayDesign": _teachers_may_design(key),
			}
			for key, label in CATEGORIES.items()
		],
		"fieldTypes": [{"value": k, "label": v} for k, v in FIELD_TYPES.items()],
		"icons": [{"value": k, "label": v} for k, v in forms_print.ICON_LABELS.items()],
		"tones": [
			{"value": k, "label": v, "color": forms_print.TONES[k][2], "background": forms_print.TONES[k][0]}
			for k, v in forms_print.TONE_LABELS.items()
		],
		"entryFor": [{"value": k, "label": v} for k, v in forms_print.ENTRY_FOR.items()],
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def list_templates(category: str, include_inactive: int = 0, persona: str = None):
	"""The forms in one file. Teachers see only the ones in use."""
	_category(category)
	filters = {"category": category}
	may_design = persona != ROLE_TEACHER or _teachers_may_design(category)
	if not may_design or not cint(include_inactive):
		filters["is_active"] = 1
	rows = frappe.get_all(
		"MS Form Template",
		filters=filters,
		fields=["name", "title", "description", "is_active", "entry_for", "modified"],
		order_by="title",
		limit_page_length=0,
	)
	counts: dict[str, int] = {}
	for e in frappe.get_all(
		"MS Form Entry",
		filters={"category": category},
		fields=["template"],
		limit_page_length=0,
	):
		counts[e.template] = counts.get(e.template, 0) + 1
	fields = {}
	for f in frappe.get_all(
		"MS Form Field",
		filters={"parenttype": "MS Form Template", "parent": ["in", [r.name for r in rows] or [""]]},
		fields=["parent", "fieldtype"],
		limit_page_length=0,
	):
		if f.fieldtype not in NO_VALUE:
			fields[f.parent] = fields.get(f.parent, 0) + 1
	subjects: dict[str, int] = {}
	for r in frappe.get_all(
		"MS Form Subject",
		filters={"parenttype": "MS Form Template", "parent": ["in", [r.name for r in rows] or [""]]},
		fields=["parent"],
		limit_page_length=0,
	):
		subjects[r.parent] = subjects.get(r.parent, 0) + 1
	return {
		"category": category,
		"label": CATEGORIES[category],
		"teachersMayFill": _teachers_may_fill(category),
		"teachersMayDesign": _teachers_may_design(category),
		"templates": [
			{
				"name": r.name,
				"title": r.title,
				"description": r.description or "",
				"isActive": cint(r.is_active),
				"entryFor": r.entry_for or "Student",
				"fields": fields.get(r.name, 0),
				"entries": counts.get(r.name, 0),
				"subjects": subjects.get(r.name, 0),
				"modified": str(r.modified),
			}
			for r in rows
		],
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def get_template(template: str, persona: str = None):
	doc = frappe.get_doc("MS Form Template", template)
	return _template_payload(doc)


PRINT_SETTINGS = {
	"entryFor": ("entry_for", tuple(forms_print.ENTRY_FOR)),
	"printTheme": ("print_theme", ("soft", "classic")),
	"printOrientation": ("print_orientation", ("Portrait", "Landscape")),
	"printLogo": ("print_logo", None),
	"printSchool": ("print_school", None),
	"printDepartment": ("print_department", None),
	"printMotto": ("print_motto", None),
	"printSignatures": ("print_signatures", None),
	"printFooter": ("print_footer", None),
}


def _apply_template(doc, data: dict, category: str):
	"""Write a design onto a template document — for saving, and for a preview
	of a design not saved yet. Returns a failure, or None."""
	doc.title = data.get("title") or doc.title or "نموذج"
	doc.category = category
	doc.description = data.get("description")
	doc.is_active = cint(data.get("isActive", 1))
	doc.print_template = data.get("printTemplate")
	if "printCss" in data:
		doc.print_css = data.get("printCss")
	for key, (fieldname, allowed) in PRINT_SETTINGS.items():
		if key in data:
			value = data.get(key) or None
			if allowed and value not in allowed:
				value = allowed[0]
			doc.set(fieldname, value)
	if doc.meta.has_field("created_by_user") and not doc.created_by_user:
		doc.created_by_user = frappe.session.user

	taken: set[str] = set()
	doc.set("fields_table", [])
	for row in data.get("fields") or []:
		fieldtype = row.get("fieldtype") or "Data"
		if fieldtype not in FIELD_TYPES:
			return fail(f"Unknown field type {fieldtype}.", f"نوع حقل غير معروف: {fieldtype}")
		label = (row.get("label") or "").strip()
		if not label:
			return fail("Every field needs a label.", "كل حقل يحتاج عنواناً.")
		tone = row.get("tone") or ""
		doc.append(
			"fields_table",
			{
				"fieldname": _clean_fieldname(label, taken, row.get("fieldname") or ""),
				"label": label,
				"fieldtype": fieldtype,
				"options": row.get("options") or "",
				"default_value": row.get("default") or "",
				"reqd": cint(row.get("reqd")),
				"width": row.get("width") or "half",
				"description": row.get("description") or "",
				"icon": row.get("icon") if row.get("icon") in forms_print.ICON_LABELS else "",
				"tone": tone if tone in forms_print.TONES else "",
				"print_rows": max(0, min(cint(row.get("print_rows")), 40)),
			},
		)
	return None


def _clean_fieldname(label: str, taken: set[str], given: str = "") -> str:
	"""A stable ASCII key for a field.

	An Arabic label yields no usable key, and a key made of Arabic letters
	would have to survive URLs, HTML ids and Jinja attribute access intact.
	Such a label gets a numbered key instead; a key already assigned is kept,
	because answers are stored against it.
	"""
	base = frappe.scrub(given or label or "")
	base = "".join(ch for ch in base if ch.isascii() and (ch.isalnum() or ch == "_")).strip("_")
	if not base or base[0].isdigit():
		base = f"field_{len(taken) + 1}"
	name = base
	i = 2
	while name in taken:
		name = f"{base}_{i}"
		i += 1
	taken.add(name)
	return name


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def save_template(payload: str | dict, persona: str = None):
	"""Create or update one form's design.

	Field keys are never reassigned once entries exist against them: a saved
	answer is tied to its key, and renaming it would orphan every answer
	already recorded.
	"""
	data = parse_json_arg(payload) or {}
	if not data.get("title"):
		return fail("A form needs a name.", "اكتب اسم النموذج.")
	category = _category(data.get("category"))
	_assert_may_design(persona, category)

	doc = (
		frappe.get_doc("MS Form Template", data["name"])
		if data.get("name")
		else frappe.new_doc("MS Form Template")
	)
	if not doc.is_new():
		# Moving a form out of the file it was allowed in is editing that file.
		_assert_may_design(persona, doc.category)
	error = _apply_template(doc, data, category)
	if error:
		return error

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _template_payload(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_template(template: str, persona: str = None):
	"""Remove a form — refused while answers are filed against it."""
	used = frappe.db.count("MS Form Entry", {"template": template})
	if used:
		return fail(
			f"{used} filled copies exist.",
			f"لا يمكن حذف النموذج: عليه {used} نموذج معبّأ. عطّله بدل حذفه.",
		)
	frappe.delete_doc("MS Form Template", template, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": template}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def duplicate_template(template: str, title: str = None, persona: str = None):
	doc = frappe.get_doc("MS Form Template", template)
	_assert_may_design(persona, doc.category)
	copy = frappe.copy_doc(doc)
	copy.title = title or f"{doc.title} (نسخة)"
	copy.created_by_user = frappe.session.user
	copy.insert(ignore_permissions=True)
	frappe.db.commit()
	return _template_payload(copy)


# --- Filling ------------------------------------------------------------------


def _teacher_students(persona: str) -> list[str]:
	"""The pupils this teacher teaches.

	`resolve_scope` carries a student list for families only; for a teacher it
	is always empty, so reading it here meant no teacher could ever find a
	student to file a form on, whatever the category's setting said.
	"""
	from match_schools.api.utils import instructor_groups

	groups = instructor_groups(resolve_scope(persona).get("instructor"), period=False)
	if not groups:
		return []
	return sorted(
		set(
			frappe.get_all(
				"Student Group Student",
				filters={"parent": ["in", groups], "parenttype": "Student Group", "active": 1},
				pluck="student",
				limit_page_length=0,
			)
		)
	)


def _teacher_groups(persona: str) -> list[str]:
	from match_schools.api.utils import instructor_groups

	return instructor_groups(resolve_scope(persona).get("instructor"), period=False) or []


def _group_students(group: str) -> list[dict]:
	"""A section's pupils, by name — the rows of a «جدول طلاب»."""
	rows = frappe.db.sql(
		"""SELECT sgs.student AS id, COALESCE(s.student_name, sgs.student) AS name
		     FROM `tabStudent Group Student` sgs
		     JOIN `tabStudent` s ON s.name = sgs.student
		    WHERE sgs.parent = %s AND sgs.parenttype = 'Student Group' AND sgs.active = 1
		    ORDER BY s.student_name""",
		group,
		as_dict=True,
	)
	return [{"id": r.id, "name": r.name, "image": None, "group": group, "groupLabel": ""} for r in rows]


def _subjects(template: str) -> list[str]:
	"""The students this form applies to."""
	return frappe.get_all(
		"MS Form Subject",
		filters={"parent": template, "parenttype": "MS Form Template"},
		pluck="student",
		order_by="idx",
		limit_page_length=0,
	)


def _students(persona: str, search: str = "", limit: int = 50, template: str = None) -> list[dict]:
	"""Students this user may file a form on.

	Back office sees the school; a teacher sees the pupils they teach — these
	files are confidential, and a teacher has no business opening the nursing
	record of a child they do not teach.
	"""
	filters = apply_period({"enabled": 1}, "Student")
	or_filters = None
	if search:
		or_filters = [
			["student_name", "like", f"%{search}%"],
			["name", "like", f"%{search}%"],
		]
	allowed = None
	if persona == ROLE_TEACHER:
		allowed = set(_teacher_students(persona))
		if not allowed:
			return []
	# Filling a form: only the students it applies to. A nursing form for the
	# children with asthma is not something to fill for the whole school.
	if template:
		subjects = set(_subjects(template))
		allowed = subjects if allowed is None else (allowed & subjects)
		if not allowed:
			return []
	if allowed is not None:
		filters["name"] = ["in", sorted(allowed)]
	rows = frappe.get_all(
		"Student",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "student_name", "image"],
		order_by="student_name",
		limit=cint(limit) or 50,
	)
	groups = {}
	for row in frappe.get_all(
		"Student Group Student",
		filters={"student": ["in", [r.name for r in rows] or [""]], "active": 1},
		fields=["student", "parent"],
		limit_page_length=0,
	):
		groups.setdefault(row.student, row.parent)
	labels = {
		g.name: g.student_group_name or g.name
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", list(set(groups.values())) or [""]]},
			fields=["name", "student_group_name"],
		)
	}
	return [
		{
			"id": r.name,
			"name": r.student_name or r.name,
			"image": r.image,
			"group": groups.get(r.name),
			"groupLabel": labels.get(groups.get(r.name), ""),
		}
		for r in rows
	]


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def students(
	search: str = "", limit: int = 50, template: str = None, group: str = None, persona: str = None
):
	"""Students to file a form on — only those the form applies to, when a
	form is given; a section's pupils, when a section is."""
	if group:
		if persona == ROLE_TEACHER and group not in _teacher_groups(persona):
			frappe.throw("هذه الشعبة ليست من شعبك.", frappe.PermissionError)
		return {"students": _group_students(group)}
	rows = _students(persona, search, limit, template)
	out = {"students": rows}
	if template:
		out["subjectsCount"] = len(_subjects(template))
	return out


def _may_see(persona: str, student: str) -> bool:
	if persona in BACK_OFFICE:
		return True
	return student in set(_teacher_students(persona))


def _may_see_entry(persona: str, doc) -> bool:
	"""A filled form: on a student the teacher teaches, on one of their
	sections, or one they filled themselves."""
	if persona in BACK_OFFICE:
		return True
	if doc.get("filled_by") == frappe.session.user:
		return True
	if doc.get("student"):
		return _may_see(persona, doc.student)
	if doc.get("student_group"):
		return doc.student_group in set(_teacher_groups(persona))
	return False


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def template_subjects(template: str, persona: str = None):
	"""The students a form applies to, with their sections. A teacher sees
	the ones they teach."""
	_assert_may_design(persona, frappe.db.get_value("MS Form Template", template, "category"))
	rows = frappe.get_all(
		"MS Form Subject",
		filters={"parent": template, "parenttype": "MS Form Template"},
		fields=["student", "student_name", "added_on"],
		order_by="idx",
		limit_page_length=0,
	)
	if persona == ROLE_TEACHER:
		mine = set(_teacher_students(persona))
		rows = [r for r in rows if r.student in mine]
	groups: dict[str, str] = {}
	for r in frappe.get_all(
		"Student Group Student",
		filters={"student": ["in", [x.student for x in rows] or [""]], "active": 1},
		fields=["student", "parent"],
		limit_page_length=0,
	):
		groups.setdefault(r.student, r.parent)
	labels = dict(
		frappe.get_all(
			"Student Group",
			filters={"name": ["in", list(set(groups.values())) or [""]]},
			fields=["name", "student_group_name"],
			as_list=True,
		)
	)
	filled = {
		r.student: r.n
		for r in frappe.db.sql(
			"""SELECT student, COUNT(*) AS n FROM `tabMS Form Entry`
			    WHERE template = %s GROUP BY student""",
			template,
			as_dict=True,
		)
	}
	return {
		"template": template,
		"students": [
			{
				"id": r.student,
				"name": r.student_name or r.student,
				"group": groups.get(r.student),
				"groupLabel": labels.get(groups.get(r.student), ""),
				"addedOn": str(r.added_on or ""),
				"entries": cint(filled.get(r.student)),
			}
			for r in rows
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def set_template_subjects(
	template: str,
	add: str | list = None,
	remove: str | list = None,
	add_group: str = None,
	persona: str = None,
):
	"""Add or remove the students a form applies to — one by one, or a whole
	section at once. A student removed keeps the forms already filed on them."""
	doc = frappe.get_doc("MS Form Template", template)
	_assert_may_design(persona, doc.category)
	current = [r.student for r in doc.subjects_table]
	adding = [x for x in (parse_json_arg(add) or []) if x]
	if add_group:
		adding += frappe.get_all(
			"Student Group Student",
			filters={"parent": add_group, "parenttype": "Student Group", "active": 1},
			pluck="student",
			limit_page_length=0,
		)
	removing = set(parse_json_arg(remove) or [])
	if persona == ROLE_TEACHER:
		# A teacher adds and removes only the pupils they teach — a colleague's
		# students on the same form are left as they are.
		mine = set(_teacher_students(persona))
		adding = [x for x in adding if x in mine]
		removing &= mine
	added = 0
	for st in dict.fromkeys(adding):
		if st not in current and st not in removing and frappe.db.exists("Student", st):
			doc.append("subjects_table", {"student": st, "added_on": nowdate()})
			current.append(st)
			added += 1
	if removing:
		doc.set("subjects_table", [r for r in doc.subjects_table if r.student not in removing])
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"added": added,
		"removed": len(removing),
		"total": len(doc.subjects_table),
		"message_ar": f"الطلاب الخاضعون الآن: {len(doc.subjects_table)}",
		"message_en": f"{len(doc.subjects_table)} students now.",
	}


def _entry_payload(doc) -> dict:
	return {
		"name": doc.name,
		"template": doc.template,
		"templateTitle": doc.template_title,
		"category": doc.category,
		"student": doc.student,
		"studentName": doc.student_name,
		"studentGroup": doc.student_group,
		"studentGroupLabel": (
			frappe.db.get_value("Student Group", doc.student_group, "student_group_name") or doc.student_group
			if doc.student_group
			else ""
		),
		"status": doc.status,
		"filledBy": doc.filled_by,
		"filledOn": str(doc.filled_on or ""),
		"notes": doc.notes or "",
		"values": {v.fieldname: v.value for v in doc.values_table},
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def list_entries(
	category: str = None,
	template: str = None,
	student: str = None,
	limit: int = 50,
	persona: str = None,
):
	"""Filled copies, newest first."""
	filters = {}
	if category:
		filters["category"] = _category(category)
	if template:
		filters["template"] = template
	if student:
		filters["student"] = student
	or_filters = None
	if persona == ROLE_TEACHER:
		# `resolve_scope` has no student list for a teacher; reading it here
		# hid every filled form from every teacher.
		mine = _teacher_students(persona)
		if student and student not in mine:
			return {"entries": []}
		if not student:
			or_filters = [
				["student", "in", mine or [""]],
				["student_group", "in", _teacher_groups(persona) or [""]],
				["filled_by", "=", frappe.session.user],
			]
	rows = frappe.get_all(
		"MS Form Entry",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name", "template", "template_title", "category", "student", "student_name",
			"student_group", "status", "filled_by", "filled_on", "modified",
		],
		order_by="modified desc",
		limit=cint(limit) or 50,
	)
	labels = dict(
		frappe.get_all(
			"Student Group",
			filters={"name": ["in", list({r.student_group for r in rows if r.student_group}) or [""]]},
			fields=["name", "student_group_name"],
			as_list=True,
		)
	)
	return {
		"entries": [
			{
				"name": r.name,
				"template": r.template,
				"templateTitle": r.template_title,
				"category": r.category,
				"student": r.student,
				"studentName": r.student_name,
				"studentGroup": r.student_group,
				"studentGroupLabel": labels.get(r.student_group) or r.student_group or "",
				"status": r.status,
				"filledBy": r.filled_by,
				"filledOn": str(r.filled_on or ""),
				"modified": str(r.modified),
			}
			for r in rows
		]
	}


# --- Reports ---------------------------------------------------------------

# Answers that can be counted: each has a closed set of values.
_COUNTABLE = ("Select", "Multi Select", "Checkbox", "Rating")


def _entry_scope(persona: str) -> list | None:
	"""A teacher's `or_filters` on MS Form Entry; None for the back office."""
	if persona != ROLE_TEACHER:
		return None
	return [
		["student", "in", _teacher_students(persona) or [""]],
		["student_group", "in", _teacher_groups(persona) or [""]],
		["filled_by", "=", frappe.session.user],
	]


def _answers(field: dict, raw: str) -> list[str]:
	"""The countable values in one answer."""
	raw = (raw or "").strip()
	if not raw:
		return []
	if field["fieldtype"] == "Multi Select":
		return [v.strip() for v in raw.replace("\n", "، ").split("، ") if v.strip()]
	if field["fieldtype"] == "Checkbox":
		return ["نعم" if raw in ("1", "true", "True") else "لا"]
	if field["fieldtype"] == "Rating":
		return [raw] if flt(raw) > 0 else []
	return [raw]


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def template_report(
	template: str = None,
	date_from: str = None,
	date_to: str = None,
	status: str = None,
	student_group: str = None,
	program: str = None,
	academic_term: str = None,
	filled_by: str = None,
	search: str = None,
	field: str = None,
	value: str = None,
	page: int = 1,
	page_size: int = 25,
	persona: str = None,
):
	"""One form's filled copies, summarised and browsable.

	Every filter narrows the whole report — the counts, the charts and the
	list — so what is summarised is always what is listed. A teacher sees
	only the copies they may open anyway.
	"""
	if not template or not frappe.db.exists("MS Form Template", template):
		return fail("Choose a form.", "اختر النموذج.")
	tdoc = frappe.get_doc("MS Form Template", template)
	fields = [f for f in _field_rows(tdoc) if f["fieldtype"] not in NO_VALUE]

	filters: dict = {"template": template}
	if status:
		filters["status"] = status
	if academic_term:
		filters["academic_term"] = academic_term
	if filled_by:
		filters["filled_by"] = filled_by
	if date_from and date_to:
		filters["filled_on"] = ["between", [f"{date_from} 00:00:00", f"{date_to} 23:59:59"]]
	elif date_from:
		filters["filled_on"] = [">=", f"{date_from} 00:00:00"]
	elif date_to:
		filters["filled_on"] = ["<=", f"{date_to} 23:59:59"]
	if search:
		filters["student_name"] = ["like", f"%{search.strip()}%"]

	rows = frappe.get_all(
		"MS Form Entry",
		filters=filters,
		or_filters=_entry_scope(persona),
		fields=[
			"name", "student", "student_name", "student_group", "status", "filled_by",
			"filled_on", "academic_term", "notes", "modified",
		],
		order_by="filled_on desc, modified desc",
		limit_page_length=0,
	)

	# A student's copy may carry no section: fall back on the section the
	# student is in, so filtering and counting by section still find it.
	students = list({r.student for r in rows if r.student and not r.student_group})
	home: dict[str, str] = {}
	for m in frappe.get_all(
		"Student Group Student",
		filters={"student": ["in", students or [""]], "parenttype": "Student Group", "active": 1},
		fields=["student", "parent"],
		limit_page_length=0,
	):
		home.setdefault(m.student, m.parent)
	for r in rows:
		r["group"] = r.student_group or home.get(r.student) or ""

	group_info = {
		g.name: g
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", list({r.group for r in rows if r.group}) or [""]]},
			fields=["name", "student_group_name", "program"],
			limit_page_length=0,
		)
	}
	if student_group:
		rows = [r for r in rows if r.group == student_group]
	if program:
		rows = [r for r in rows if group_info.get(r.group) and group_info[r.group].program == program]

	# Answers, one query for every copy in view.
	values: dict[str, dict[str, str]] = {}
	for v in frappe.get_all(
		"MS Form Value",
		filters={"parenttype": "MS Form Entry", "parent": ["in", [r.name for r in rows] or [""]]},
		fields=["parent", "fieldname", "value"],
		limit_page_length=0,
	):
		values.setdefault(v.parent, {})[v.fieldname] = v.value or ""

	by_field = {f["fieldname"]: f for f in fields}
	if field and value is not None and value != "" and field in by_field:
		f = by_field[field]
		rows = [r for r in rows if value in _answers(f, values.get(r.name, {}).get(field, ""))]

	# --- Summary over everything in view.
	users = list({r.filled_by for r in rows if r.filled_by})
	names = dict(
		frappe.get_all(
			"User", filters={"name": ["in", users or [""]]}, fields=["name", "full_name"], as_list=True
		)
	)
	by_month: dict[str, int] = {}
	by_group: dict[str, int] = {}
	by_filler: dict[str, int] = {}
	for r in rows:
		month = str(r.filled_on or r.modified or "")[:7]
		if month:
			by_month[month] = by_month.get(month, 0) + 1
		if r.group:
			by_group[r.group] = by_group.get(r.group, 0) + 1
		if r.filled_by:
			by_filler[r.filled_by] = by_filler.get(r.filled_by, 0) + 1

	summaries = []
	for f in fields:
		answers = [values.get(r.name, {}).get(f["fieldname"], "") for r in rows]
		answered = sum(1 for a in answers if str(a).strip() and not (f["fieldtype"] == "Checkbox" and a == "0"))
		item = {
			"fieldname": f["fieldname"],
			"label": f["label"],
			"fieldtype": f["fieldtype"],
			"answered": answered if f["fieldtype"] != "Checkbox" else len([a for a in answers if a]),
			"total": len(rows),
		}
		if f["fieldtype"] in _COUNTABLE:
			counts: dict[str, int] = {}
			for a in answers:
				for x in _answers(f, a):
					counts[x] = counts.get(x, 0) + 1
			order = (
				["نعم", "لا"]
				if f["fieldtype"] == "Checkbox"
				else [o.strip() for o in (f["options"] or "").split("\n") if o.strip()]
			)
			item["distribution"] = sorted(
				[{"value": k, "count": c} for k, c in counts.items()],
				key=lambda d: (order.index(d["value"]) if d["value"] in order else len(order), -d["count"]),
			)
			if f["fieldtype"] == "Rating":
				nums = [flt(a) for a in answers if flt(a) > 0]
				item["average"] = round(sum(nums) / len(nums), 2) if nums else None
		elif f["fieldtype"] == "Number":
			nums = [flt(a) for a in answers if str(a).strip() not in ("", None)]
			if nums:
				item["stats"] = {
					"average": round(sum(nums) / len(nums), 2),
					"min": min(nums),
					"max": max(nums),
					"sum": round(sum(nums), 2),
				}
		summaries.append(item)

	page = max(cint(page) or 1, 1)
	page_size = min(max(cint(page_size) or 25, 1), 100)
	start = (page - 1) * page_size
	listed = rows[start : start + page_size]

	def group_label(name: str) -> str:
		g = group_info.get(name)
		return (g.student_group_name if g else name) or ""

	return {
		"template": {
			"name": tdoc.name,
			"title": tdoc.title,
			"category": tdoc.category,
			"categoryLabel": CATEGORIES.get(tdoc.category, tdoc.category),
			"entryFor": tdoc.get("entry_for") or "Student",
			"fields": fields,
		},
		"kpis": {
			"entries": len(rows),
			"completed": sum(1 for r in rows if r.status == "مكتمل"),
			"drafts": sum(1 for r in rows if r.status != "مكتمل"),
			"students": len({r.student for r in rows if r.student}),
			"groups": len(by_group),
			"fillers": len(by_filler),
			"first": str(min((r.filled_on for r in rows if r.filled_on), default="") or ""),
			"last": str(max((r.filled_on for r in rows if r.filled_on), default="") or ""),
		},
		"byMonth": [{"month": k, "count": v} for k, v in sorted(by_month.items())],
		"byGroup": sorted(
			[{"group": k, "label": group_label(k), "count": v} for k, v in by_group.items()],
			key=lambda d: -d["count"],
		),
		"byFiller": sorted(
			[{"user": k, "name": names.get(k) or k, "count": v} for k, v in by_filler.items()],
			key=lambda d: -d["count"],
		),
		"fields": summaries,
		"entries": [
			{
				"name": r.name,
				"student": r.student,
				"studentName": r.student_name or "",
				"group": r.group,
				"groupLabel": group_label(r.group),
				"status": r.status,
				"filledBy": r.filled_by,
				"filledByName": names.get(r.filled_by) or r.filled_by or "",
				"filledOn": str(r.filled_on or ""),
				"academicTerm": r.academic_term or "",
				"values": values.get(r.name, {}),
			}
			for r in listed
		],
		"total": len(rows),
		"page": page,
		"pageSize": page_size,
		"filterOptions": {
			"groups": sorted(
				[{"value": k, "label": group_label(k)} for k in by_group],
				key=lambda d: d["label"],
			),
			"programs": sorted({g.program for g in group_info.values() if g.program}),
			"fillers": [{"value": k, "label": names.get(k) or k} for k in by_filler],
			"terms": sorted({r.academic_term for r in rows if r.academic_term}),
			"statuses": ["مكتمل", "مسودة"],
		},
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def get_entry(entry: str, persona: str = None):
	doc = frappe.get_doc("MS Form Entry", entry)
	if not _may_see_entry(persona, doc):
		frappe.throw(frappe._("You are not allowed to open this form."), frappe.PermissionError)
	return _entry_payload(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def save_entry(payload: str | dict, persona: str = None):
	"""Record one filled form.

	Values are stored one row per field rather than as a blob, so a school can
	report on an answer later, and a field removed from the design keeps the
	answers already given under it.
	"""
	data = parse_json_arg(payload) or {}
	if not data.get("template"):
		return fail("Choose a form.", "اختر النموذج.")
	template = frappe.get_doc("MS Form Template", data["template"])
	entry_for = template.get("entry_for") or "Student"
	if data.get("name"):
		existing = frappe.get_doc("MS Form Entry", data["name"])
		if not _may_see_entry(persona, existing):
			frappe.throw(frappe._("You are not allowed to edit this form."), frappe.PermissionError)

	if entry_for == "Student":
		if not data.get("student"):
			return fail("Choose a student.", "اختر الطالب.")
		if not _may_see(persona, data["student"]):
			frappe.throw(frappe._("You are not allowed to file this form."), frappe.PermissionError)
		# A form is filed only on a student it applies to. Existing entries
		# stay editable, so a student later removed keeps their record.
		if not data.get("name") and data["student"] not in set(_subjects(template.name)):
			return fail(
				"This student is not on this form's list.",
				"هذا الطالب ليس من الطلاب الخاضعين لهذا النموذج — أضفه من تبويب «الطلاب الخاضعون».",
			)
	elif entry_for == "Section":
		if not data.get("studentGroup"):
			return fail("Choose a section.", "اختر الشعبة.")
		if persona == ROLE_TEACHER and data["studentGroup"] not in set(_teacher_groups(persona)):
			frappe.throw("هذه الشعبة ليست من شعبك.", frappe.PermissionError)
	if persona == ROLE_TEACHER and not _teachers_may_fill(template.category):
		return fail(
			"Teachers may not fill forms in this file.",
			f"تعبئة نماذج {CATEGORIES.get(template.category, '')} مقصورة على الإدارة — راجع إعدادات القسم.",
		)
	values = data.get("values") or {}
	if isinstance(values, str):
		values = parse_json_arg(values) or {}

	missing = [
		f.label
		for f in template.fields_table
		if cint(f.reqd)
		and f.fieldtype not in NO_VALUE
		and not str(values.get(f.fieldname, "")).strip()
	]
	if missing and data.get("status") == "مكتمل":
		return fail(
			"Required fields are empty.",
			"حقول مطلوبة فارغة: " + "، ".join(missing[:6]),
		)

	doc = (
		frappe.get_doc("MS Form Entry", data["name"])
		if data.get("name")
		else frappe.new_doc("MS Form Entry")
	)
	doc.template = template.name
	doc.template_title = template.title
	doc.category = template.category
	if entry_for == "Student":
		doc.student = data["student"]
		doc.student_name = frappe.db.get_value("Student", data["student"], "student_name")
		doc.student_group = data.get("studentGroup") or None
	else:
		doc.student = None
		doc.student_name = None
		doc.student_group = (data.get("studentGroup") or None) if entry_for == "Section" else None
	doc.status = data.get("status") or "مسودة"
	doc.notes = data.get("notes")
	doc.filled_by = doc.filled_by or frappe.session.user
	doc.filled_on = doc.filled_on or now_datetime()
	doc.academic_year = doc.academic_year or get_default_academic_year()
	doc.academic_term = doc.academic_term or get_default_academic_term()

	doc.set("values_table", [])
	for f in template.fields_table:
		if f.fieldtype in NO_VALUE:
			continue
		value = values.get(f.fieldname, "")
		if isinstance(value, (list, dict)):
			value = json.dumps(value, ensure_ascii=False)
		doc.append(
			"values_table",
			{
				"fieldname": f.fieldname,
				"label": f.label,
				"fieldtype": f.fieldtype,
				"value": "" if value is None else str(value),
			},
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _entry_payload(doc)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_entry(entry: str, persona: str = None):
	frappe.delete_doc("MS Form Entry", entry, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": entry}


# --- Printing -----------------------------------------------------------------


def _print_payload(template_doc, entry_doc, blank: bool = False) -> str:
	return forms_print.render(template_doc, entry_doc, _field_rows(template_doc), blank)


def _render_or_fail(template_doc, entry_doc, blank: bool = False):
	try:
		return _print_payload(template_doc, entry_doc, blank), None
	except Exception as exc:
		# Jinja errors arrive as HTML with a traceback; a designer needs the
		# sentence, not the page.
		reason = re.sub(r"<[^>]+>", " ", str(exc)).strip()
		return None, fail(
			"The print design could not be rendered.",
			f"خطأ في تصميم الطباعة: {reason[:220]}",
		)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def print_entry(entry: str, persona: str = None):
	"""The filled form as printable HTML."""
	doc = frappe.get_doc("MS Form Entry", entry)
	if not _may_see_entry(persona, doc):
		frappe.throw(frappe._("You are not allowed to print this form."), frappe.PermissionError)
	template = frappe.get_doc("MS Form Template", doc.template)
	html, error = _render_or_fail(template, doc)
	if error:
		return error
	who = doc.student_name or (
		frappe.db.get_value("Student Group", doc.student_group, "student_group_name") if doc.student_group else ""
	)
	return {"html": html, "title": f"{doc.template_title}" + (f" — {who}" if who else "")}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def print_blank(template: str, persona: str = None):
	"""The form with no answers — dotted lines and empty boxes, to print and
	fill by hand."""
	doc = frappe.get_doc("MS Form Template", template)
	html, error = _render_or_fail(doc, frappe.new_doc("MS Form Entry"), blank=True)
	if error:
		return error
	return {"html": html, "title": doc.title}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def preview_print(
	template: str = None,
	html: str = None,
	css: str = None,
	payload: str | dict = None,
	blank: int = 0,
	persona: str = None,
):
	"""What a design looks like before it is saved.

	With `payload` (the designer's whole draft) the preview follows every
	unsaved change — fields, settings, design and CSS — and needs no saved
	form at all. It is drawn on the latest filled copy when there is one, and
	blank otherwise, which is how the paper form looks anyway.
	"""
	data = parse_json_arg(payload) or {}
	if template:
		doc = frappe.get_doc("MS Form Template", template)
	else:
		doc = frappe.new_doc("MS Form Template")
	category = data.get("category") or doc.get("category")
	_assert_may_design(persona, category)
	if data:
		error = _apply_template(doc, data, _category(category))
		if error:
			return error
	if html is not None:
		doc.print_template = html
	if css is not None:
		doc.print_css = css
	latest = (
		frappe.get_all(
			"MS Form Entry", filters={"template": template}, order_by="modified desc", limit=1, pluck="name"
		)
		if template and not cint(blank)
		else []
	)
	entry = frappe.get_doc("MS Form Entry", latest[0]) if latest else frappe.new_doc("MS Form Entry")
	out, error = _render_or_fail(doc, entry, blank=not latest)
	if error:
		return error
	return {"html": out, "blank": not latest}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def upload_logo(persona: str = None):
	"""A logo for a form's letterhead. Returned as a URL the design keeps; the
	printed page carries the image itself, so it prints anywhere."""
	upload = frappe.request.files.get("file") if frappe.request else None
	if not upload:
		return fail("No file.", "اختر صورة الشعار.")
	name = upload.filename or "logo.png"
	if not re.search(r"\.(png|jpe?g|webp|gif|svg)$", name, re.I):
		return fail("Not an image.", "الشعار يجب أن يكون صورة (PNG أو JPG أو SVG).")
	content = upload.stream.read()
	if len(content) > 3 * 1024 * 1024:
		return fail("Too large.", "حجم الشعار أكبر من 3 ميغابايت.")
	f = frappe.get_doc(
		{"doctype": "File", "file_name": name, "content": content, "is_private": 0, "folder": "Home"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"url": f.file_url}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def set_category_settings(
	category: str,
	teachers_may_fill: int = None,
	teachers_may_design: int = None,
	persona: str = None,
):
	"""Who may file forms in one file, and who may add and edit its forms.
	A setting left out is left as it is."""
	_category(category)
	if teachers_may_fill is not None:
		frappe.db.set_default(_teachers_key(category), "1" if cint(teachers_may_fill) else "0")
	if teachers_may_design is not None:
		frappe.db.set_default(_design_key(category), "1" if cint(teachers_may_design) else "0")
	frappe.db.commit()
	return {
		"category": category,
		"teachersMayFill": _teachers_may_fill(category),
		"teachersMayDesign": _teachers_may_design(category),
	}


# --- Converting between the fields and the print design -----------------------


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def design_from_fields(
	fields: str | list = None,
	template: str = None,
	category: str = None,
	entry_for: str = None,
	persona: str = None,
):
	"""«تحويل الحقول إلى تصميم طباعة»: a ready design from the fields.

	Takes the fields as they stand in the designer (saved or not). Nothing is
	saved: the designer shows it, and the school edits and saves it like any
	design. The look comes from the theme, so no CSS is needed to start.
	"""
	if template:
		category = frappe.db.get_value("MS Form Template", template, "category")
		entry_for = entry_for or frappe.db.get_value("MS Form Template", template, "entry_for")
	_assert_may_design(persona, category)
	rows = parse_json_arg(fields)
	if rows is None and template:
		rows = _field_rows(frappe.get_doc("MS Form Template", template))
	rows = [r for r in (rows or []) if (r.get("label") or "").strip()]
	if not rows:
		return fail("Add fields first.", "أضف حقولاً أولاً، ثم حوّلها إلى تصميم.")
	return {"html": forms_print.design_from_fields(rows, entry_for or "Student"), "css": ""}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def fields_from_design(
	html: str, fields: str | list = None, category: str = None, persona: str = None
):
	"""«تحويل التصميم إلى حقول»: the fields a design places, in its order.

	Reads `box`, `inline`, `checks` and `field` — `("label", "type", "a|b")` —
	and `section("…")`. A field already there under the same label keeps its
	key, colour, icon and width, so the answers filed under it stay attached;
	`{{ values.key }}` keeps an existing field by key. A label ending in `*`
	is required.
	"""
	_assert_may_design(persona, category)
	current = [r for r in (parse_json_arg(fields) or []) if (r.get("label") or "").strip()]
	by_label = {r["label"].strip(): r for r in current}
	by_name = {r.get("fieldname"): r for r in current if r.get("fieldname")}

	out: list[dict] = []
	seen: set[str] = set()
	unknown: list[str] = []
	for m in forms_print.CALL.finditer(html or ""):
		kind, label = m.group(1), (m.group(3) or "").strip()
		if not label:
			continue
		if kind == "section":
			if ("§" + label) in seen or label == "ملاحظات":
				continue
			seen.add("§" + label)
			existing = by_label.get(label)
			out.append({
				**(existing or {}),
				"label": label,
				"fieldtype": existing["fieldtype"] if existing and existing.get("fieldtype") in LAYOUT_TYPES else "Section",
				"options": "",
			})
			continue
		reqd = label.endswith("*")
		label = label.rstrip(" *")
		if label in seen:
			continue
		seen.add(label)
		fieldtype = (m.group(5) or "").strip()
		if fieldtype and fieldtype not in FIELD_TYPES:
			# An Arabic type name is accepted too.
			fieldtype = next((k for k, v in FIELD_TYPES.items() if v == fieldtype), "")
			if not fieldtype:
				unknown.append(m.group(5))
		existing = by_label.get(label)
		options = "\n".join(o.strip() for o in (m.group(7) or "").split("|") if o.strip())
		fieldtype = fieldtype or (existing or {}).get("fieldtype") or "Data"
		if fieldtype in ("Text Block", "Student Table"):
			options = (existing or {}).get("options") or options
		out.append({
			**(existing or {}),
			"label": label,
			"fieldtype": fieldtype,
			"options": options or (existing or {}).get("options") or "",
			# The design is the source: no `*`, not required.
			"reqd": 1 if reqd else 0,
			"width": (existing or {}).get("width") or ("half" if kind == "inline" else "full"),
		})
	# Fields the design shows by key rather than through a helper.
	for m in forms_print.VALUE.finditer(html or ""):
		key = m.group(1) or m.group(2)
		f = by_name.get(key)
		if f and f["label"] not in seen:
			seen.add(f["label"])
			out.append(dict(f))

	if not out:
		return fail(
			"No fields found in the design.",
			'لم أجد حقولاً في التصميم. ضع كل حقل هكذا: {{ box("اسم الحقل", "Long Text") }} — راجع «مساعدة».',
		)
	kept = sum(1 for f in out if f.get("fieldname"))
	dropped = [r["label"] for r in current if r["label"] not in seen and ("§" + r["label"]) not in seen]
	return {
		"fields": out,
		"kept": kept,
		"added": len(out) - kept,
		"dropped": dropped,
		"unknownTypes": unknown,
	}
