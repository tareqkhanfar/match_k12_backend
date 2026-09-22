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
from frappe.utils import cint, escape_html, now_datetime, nowdate

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
}
LAYOUT_TYPES = ("Section", "Heading")


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
		fields=["name", "title", "description", "is_active", "modified"],
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
		if f.fieldtype not in LAYOUT_TYPES:
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
	doc.title = data["title"]
	doc.category = category
	doc.description = data.get("description")
	doc.is_active = cint(data.get("isActive", 1))
	doc.print_template = data.get("printTemplate")
	if "printCss" in data:
		doc.print_css = data.get("printCss")
	if not doc.created_by_user:
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
			},
		)

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
def students(search: str = "", limit: int = 50, template: str = None, persona: str = None):
	"""Students to file a form on — only those the form applies to, when a
	form is given."""
	rows = _students(persona, search, limit, template)
	out = {"students": rows}
	if template:
		out["subjectsCount"] = len(_subjects(template))
	return out


def _may_see(persona: str, student: str) -> bool:
	if persona in BACK_OFFICE:
		return True
	return student in set(_teacher_students(persona))


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
	if persona == ROLE_TEACHER:
		mine = resolve_scope(persona).get("students") or []
		filters["student"] = ["in", mine or [""]]
		if student and student not in mine:
			return {"entries": []}
		if student:
			filters["student"] = student
	rows = frappe.get_all(
		"MS Form Entry",
		filters=filters,
		fields=[
			"name", "template", "template_title", "category", "student", "student_name",
			"student_group", "status", "filled_by", "filled_on", "modified",
		],
		order_by="modified desc",
		limit=cint(limit) or 50,
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
				"status": r.status,
				"filledBy": r.filled_by,
				"filledOn": str(r.filled_on or ""),
				"modified": str(r.modified),
			}
			for r in rows
		]
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def get_entry(entry: str, persona: str = None):
	doc = frappe.get_doc("MS Form Entry", entry)
	if not _may_see(persona, doc.student):
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
	if not data.get("student"):
		return fail("Choose a student.", "اختر الطالب.")
	if not _may_see(persona, data["student"]):
		frappe.throw(frappe._("You are not allowed to file this form."), frappe.PermissionError)

	template = frappe.get_doc("MS Form Template", data["template"])
	# A form is filed only on a student it applies to. Existing entries stay
	# editable, so a student later removed from the list keeps their record.
	if not data.get("name") and data["student"] not in set(_subjects(template.name)):
		return fail(
			"This student is not on this form's list.",
			"هذا الطالب ليس من الطلاب الخاضعين لهذا النموذج — أضفه من تبويب «الطلاب الخاضعون».",
		)
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
		and f.fieldtype not in LAYOUT_TYPES
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
	doc.student = data["student"]
	doc.student_name = frappe.db.get_value("Student", data["student"], "student_name")
	doc.student_group = data.get("studentGroup") or None
	doc.status = data.get("status") or "مسودة"
	doc.notes = data.get("notes")
	doc.filled_by = doc.filled_by or frappe.session.user
	doc.filled_on = doc.filled_on or now_datetime()
	doc.academic_year = doc.academic_year or get_default_academic_year()
	doc.academic_term = doc.academic_term or get_default_academic_term()

	doc.set("values_table", [])
	for f in template.fields_table:
		if f.fieldtype in LAYOUT_TYPES:
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


def _default_print_template() -> str:
	"""What a form prints as before anyone designs it.

	A school should be able to print the day it creates a form, not after
	someone writes HTML — the designed template replaces this when there is one.
	"""
	return """<div class="ms-form">
  <h1>{{ template.title }}</h1>
  <table class="head">
    <tr><td><b>الطالب:</b> {{ entry.student_name }}</td><td><b>الشعبة:</b> {{ entry.student_group or "—" }}</td></tr>
    <tr><td><b>التاريخ:</b> {{ filled_on }}</td><td><b>عبّأه:</b> {{ entry.filled_by }}</td></tr>
  </table>
  {% for field in fields %}
    {% if field.fieldtype == "Section" or field.fieldtype == "Heading" %}
      <h2>{{ field.label }}</h2>
    {% else %}
      <div class="row"><span class="label">{{ field.label }}:</span>
        <span class="value">{{ values.get(field.fieldname) or "—" }}</span></div>
    {% endif %}
  {% endfor %}
  {% if entry.notes %}<h2>ملاحظات</h2><p>{{ entry.notes }}</p>{% endif %}
</div>"""


PRINT_STYLE = """<style>
  @page { size: A4; margin: 14mm; }
  body { font-family: "Tajawal", "Segoe UI", sans-serif; direction: rtl; color: #111; }
  .ms-form h1 { font-size: 20px; text-align: center; margin: 0 0 12px; }
  .ms-form h2 { font-size: 14px; background: #eef2f9; padding: 6px 8px; margin: 14px 0 6px; border-radius: 4px; }
  .ms-form table.head { width: 100%; border-collapse: collapse; margin-bottom: 10px; font-size: 12px; }
  .ms-form table.head td { border: 1px solid #ccd; padding: 5px 7px; }
  .ms-form .row { display: flex; gap: 6px; padding: 4px 2px; border-bottom: 1px dotted #ccd; font-size: 12px; }
  .ms-form .label { font-weight: 700; min-width: 160px; }
  .ms-form table.grid { width: 100%; border-collapse: collapse; font-size: 12px; margin: 6px 0; }
  .ms-form table.grid th, .ms-form table.grid td { border: 1px solid #ccd; padding: 4px 6px; }
  @media print { .no-print { display: none; } }
</style>"""


def _css(css: str | None) -> str:
	"""A form's own print CSS, after the base style so it can override it.

	A stray `</style>` would end the block and let the rest into the page as
	markup, so it is taken out.
	"""
	css = (css or "").strip()
	if not css:
		return ""
	return "<style>\n" + css.replace("</style", "") + "\n</style>"


def _render(template_doc, entry_doc) -> str:
	fields = _field_rows(template_doc)
	values = {v.fieldname: v.value for v in entry_doc.values_table}
	# Stored answers are text; a printed page should read as a page. A tick is
	# "نعم", a rating is out of five, and a timestamp loses its microseconds.
	pretty = dict(values)
	for f in fields:
		raw = values.get(f["fieldname"], "")
		if f["fieldtype"] == "Checkbox":
			pretty[f["fieldname"]] = "نعم" if cint(raw) else ("لا" if raw != "" else "")
		elif f["fieldtype"] == "Rating":
			pretty[f["fieldname"]] = f"{cint(raw)} / 5" if raw else ""
		elif f["fieldtype"] == "Datetime" and raw:
			pretty[f["fieldname"]] = str(raw)[:16]
	for f in fields:
		if f["fieldtype"] == "Table" and values.get(f["fieldname"]):
			try:
				rows = json.loads(values[f["fieldname"]])
				columns = [c.strip() for c in (f["options"] or "").split("\n") if c.strip()]
				head = "".join(f"<th>{frappe.utils.escape_html(c)}</th>" for c in columns)
				body = "".join(
					"<tr>"
					+ "".join(
						f"<td>{frappe.utils.escape_html(str(row.get(c, '')))}</td>" for c in columns
					)
					+ "</tr>"
					for row in rows
				)
				pretty[f["fieldname"]] = f'<table class="grid"><tr>{head}</tr>{body}</table>'
			except Exception:
				pass
	filled = entry_doc.filled_on
	by_label = {f["label"]: f for f in fields}
	by_name = {f["fieldname"]: f for f in fields}

	def field(label, *_args, **_kwargs):
		"""A field's answer in a design, found by its label — or its key.

		The extra arguments (type, options) describe the field for «تحويل
		التصميم إلى حقول»; printing ignores them.
		"""
		f = by_label.get(str(label).rstrip(" *")) or by_name.get(str(label))
		if not f:
			return ""
		value = pretty.get(f["fieldname"])
		return value if value not in (None, "") else "—"

	def section(label, *_args, **_kwargs):
		return f'<h2 class="section">{escape_html(str(label))}</h2>'

	context = {
		"filled_on": str(filled)[:16] if filled else "",
		"entry": entry_doc,
		"template": template_doc,
		"fields": fields,
		"values": pretty,
		"school": frappe.db.get_default("company") or "",
		"field": field,
		"section": section,
	}
	html = template_doc.print_template or _default_print_template()
	return frappe.render_template(html, context)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def print_entry(entry: str, persona: str = None):
	"""The filled form as printable HTML."""
	doc = frappe.get_doc("MS Form Entry", entry)
	if not _may_see(persona, doc.student):
		frappe.throw(frappe._("You are not allowed to print this form."), frappe.PermissionError)
	template = frappe.get_doc("MS Form Template", doc.template)
	try:
		body = _render(template, doc)
	except Exception as exc:
		return fail(
			"The print template could not be rendered.",
			f"تعذّر تجهيز قالب الطباعة: {str(exc)[:160]}",
		)
	return {
		"html": PRINT_STYLE + _css(template.get("print_css")) + body,
		"title": f"{doc.template_title} — {doc.student_name}",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def preview_print(template: str, html: str = None, css: str = None, persona: str = None):
	"""What a print design looks like, before it is saved or filled.

	Rendered against the latest filled copy when there is one, and against
	sample answers when there is not, so a designer sees a real page rather
	than a skeleton of empty rows.
	"""
	doc = frappe.get_doc("MS Form Template", template)
	_assert_may_design(persona, doc.category)
	if html is not None:
		doc.print_template = html
	if css is not None:
		doc.print_css = css
	latest = frappe.get_all(
		"MS Form Entry", filters={"template": template}, order_by="modified desc", limit=1, pluck="name"
	)
	if latest:
		entry = frappe.get_doc("MS Form Entry", latest[0])
	else:
		entry = frappe.new_doc("MS Form Entry")
		entry.student_name = "اسم الطالب"
		entry.student_group = "الشعبة"
		entry.filled_by = frappe.session.user
		entry.filled_on = now_datetime()
		entry.notes = "ملاحظات تجريبية"
		for f in doc.fields_table:
			if f.fieldtype in LAYOUT_TYPES:
				continue
			entry.append(
				"values_table",
				{
					"fieldname": f.fieldname,
					"label": f.label,
					"fieldtype": f.fieldtype,
					"value": "نموذج" if f.fieldtype not in ("Number", "Rating") else "3",
				},
			)
	try:
		return {"html": PRINT_STYLE + _css(doc.get("print_css")) + _render(doc, entry)}
	except Exception as exc:
		# Jinja errors arrive as HTML with a traceback; a designer needs the
		# sentence, not the page.
		import re as _re

		reason = _re.sub(r"<[^>]+>", " ", str(exc)).strip()
		return fail(
			"The print template could not be rendered.",
			f"خطأ في القالب: {reason[:200]}",
		)


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

# `{{ field("الوزن", "Number") }}` / `{{ section("العلامات الحيوية") }}` — the
# markup a design uses to place a field, and what «تحويل التصميم إلى حقول»
# reads back. Up to three quoted arguments: label, type, options.
_CALL = re.compile(
	r"""\{\{\s*(field|section)\(\s*(["'])(.*?)\2"""
	r"""(?:\s*,\s*(["'])(.*?)\4)?"""
	r"""(?:\s*,\s*(["'])(.*?)\6)?\s*\)\s*\}\}""",
	re.S,
)
# `{{ values.key }}` or `{{ values.get("key") }}` — an existing field by key.
_VALUE = re.compile(r"""values(?:\.get\(\s*["']([a-z0-9_]+)["']|\.([a-z0-9_]+))""")

DEFAULT_PRINT_CSS = """/* الألوان والخطوط — غيّرها كما تشاء */
.ms-form { font-size: 12.5px; }
.ms-form .ms-head { text-align: center; border-bottom: 2px solid #1a224b; padding-bottom: 8px; margin-bottom: 10px; }
.ms-form .ms-head .school { font-size: 13px; color: #555; }
.ms-form .ms-head h1 { margin: 4px 0 0; }
.ms-form table.info, .ms-form table.fields { width: 100%; border-collapse: collapse; margin: 6px 0 10px; }
.ms-form table.info td { border: 1px solid #ccd; padding: 5px 8px; }
.ms-form table.fields th { width: 32%; background: #f5f7fb; text-align: right; font-weight: 700; }
.ms-form table.fields th, .ms-form table.fields td { border: 1px solid #ccd; padding: 6px 8px; vertical-align: top; }
.ms-form h2.section { font-size: 14px; background: #eef2f9; padding: 6px 8px; margin: 14px 0 6px; border-radius: 4px; }
.ms-form .signatures { display: flex; justify-content: space-between; margin-top: 36px; }
.ms-form .signatures div { width: 30%; text-align: center; border-top: 1px solid #333; padding-top: 4px; }
"""


def _quote(text: str) -> str:
	return '"' + str(text).replace('"', "'") + '"'


def _field_call(f: dict) -> str:
	"""The design markup for one field — carrying its type and options, so the
	design can be turned back into the same fields."""
	args = [_quote(f["label"] + (" *" if cint(f.get("reqd")) else ""))]
	fieldtype = f.get("fieldtype") or "Data"
	options = [o.strip() for o in (f.get("options") or "").split("\n") if o.strip()]
	if fieldtype != "Data" or options:
		args.append(_quote(fieldtype))
	if options:
		args.append(_quote("|".join(options)))
	return "{{ field(" + ", ".join(args) + ") }}"


def _design_from_fields(fields: list[dict]) -> str:
	lines = [
		'<div class="ms-form">',
		'  <header class="ms-head">',
		'    <div class="school">{{ school }}</div>',
		"    <h1>{{ template.title }}</h1>",
		"  </header>",
		'  <table class="info">',
		'    <tr><td><b>الطالب:</b> {{ entry.student_name }}</td><td><b>الشعبة:</b> {{ entry.student_group or "—" }}</td></tr>',
		"    <tr><td><b>التاريخ:</b> {{ filled_on }}</td><td><b>عبّأه:</b> {{ entry.filled_by }}</td></tr>",
		"  </table>",
	]
	open_table = False
	for f in fields:
		fieldtype = f.get("fieldtype") or "Data"
		if fieldtype in LAYOUT_TYPES:
			if open_table:
				lines.append("  </table>")
				open_table = False
			lines.append("  {{ section(" + _quote(f["label"]) + ") }}")
			continue
		if not open_table:
			lines.append('  <table class="fields">')
			open_table = True
		lines.append(
			f"    <tr><th>{escape_html(f['label'])}</th><td>{_field_call(f)}</td></tr>"
		)
	if open_table:
		lines.append("  </table>")
	lines += [
		'  {% if entry.notes %}{{ section("ملاحظات") }}<p>{{ entry.notes }}</p>{% endif %}',
		'  <div class="signatures">',
		"    <div>توقيع المختص</div>",
		"    <div>توقيع ولي الأمر</div>",
		"    <div>ختم المدرسة</div>",
		"  </div>",
		"</div>",
	]
	return "\n".join(lines)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def design_from_fields(
	fields: str | list = None, template: str = None, category: str = None, persona: str = None
):
	"""«تحويل الحقول إلى تصميم طباعة»: a ready print design from the fields.

	Takes the fields as they stand in the designer (saved or not), and returns
	a design plus a starting CSS. Nothing is saved: the designer shows it, and
	the school edits and saves it like any design.
	"""
	if template:
		category = frappe.db.get_value("MS Form Template", template, "category")
	_assert_may_design(persona, category)
	rows = parse_json_arg(fields)
	if rows is None and template:
		rows = _field_rows(frappe.get_doc("MS Form Template", template))
	rows = [r for r in (rows or []) if (r.get("label") or "").strip()]
	if not rows:
		return fail("Add fields first.", "أضف حقولاً أولاً، ثم حوّلها إلى تصميم.")
	return {"html": _design_from_fields(rows), "css": DEFAULT_PRINT_CSS}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def fields_from_design(
	html: str, fields: str | list = None, category: str = None, persona: str = None
):
	"""«تحويل التصميم إلى حقول»: the fields a design places, in its order.

	Reads `{{ field("…", "type", "a|b") }}` and `{{ section("…") }}`. A field
	that already exists under the same label keeps its key, so the answers
	already filed under it stay attached; `{{ values.key }}` keeps an existing
	field by key. A label ending in `*` is a required field.
	"""
	_assert_may_design(persona, category)
	current = [r for r in (parse_json_arg(fields) or []) if (r.get("label") or "").strip()]
	by_label = {r["label"].strip(): r for r in current}
	by_name = {r.get("fieldname"): r for r in current if r.get("fieldname")}

	out: list[dict] = []
	seen: set[str] = set()
	unknown: list[str] = []
	for m in _CALL.finditer(html or ""):
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
		out.append({
			**(existing or {}),
			"label": label,
			"fieldtype": fieldtype or (existing or {}).get("fieldtype") or "Data",
			"options": options or (existing or {}).get("options") or "",
			# The design is the source: no `*`, not required.
			"reqd": 1 if reqd else 0,
			"width": (existing or {}).get("width") or "half",
		})
	# Fields the design shows by key rather than through field().
	for m in _VALUE.finditer(html or ""):
		key = m.group(1) or m.group(2)
		f = by_name.get(key)
		if f and f["label"] not in seen:
			seen.add(f["label"])
			out.append(dict(f))

	if not out:
		return fail(
			"No fields found in the design.",
			'لم أجد حقولاً في التصميم. ضع كل حقل هكذا: {{ field("اسم الحقل", "Number") }} — راجع «مساعدة».',
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
