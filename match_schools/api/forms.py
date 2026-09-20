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

import frappe
from frappe.utils import cint, now_datetime

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
			{"key": key, "label": label, "forms": counts.get(key, 0)}
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
	if persona == ROLE_TEACHER or not cint(include_inactive):
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
	return {
		"category": category,
		"label": CATEGORIES[category],
		"templates": [
			{
				"name": r.name,
				"title": r.title,
				"description": r.description or "",
				"isActive": cint(r.is_active),
				"fields": fields.get(r.name, 0),
				"entries": counts.get(r.name, 0),
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
@ms_endpoint(*BACK_OFFICE)
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

	doc = (
		frappe.get_doc("MS Form Template", data["name"])
		if data.get("name")
		else frappe.new_doc("MS Form Template")
	)
	doc.title = data["title"]
	doc.category = category
	doc.description = data.get("description")
	doc.is_active = cint(data.get("isActive", 1))
	doc.print_template = data.get("printTemplate")
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
@ms_endpoint(*BACK_OFFICE)
def duplicate_template(template: str, title: str = None, persona: str = None):
	doc = frappe.get_doc("MS Form Template", template)
	copy = frappe.copy_doc(doc)
	copy.title = title or f"{doc.title} (نسخة)"
	copy.created_by_user = frappe.session.user
	copy.insert(ignore_permissions=True)
	frappe.db.commit()
	return _template_payload(copy)


# --- Filling ------------------------------------------------------------------


def _students(persona: str, search: str = "", limit: int = 50) -> list[dict]:
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
	if persona == ROLE_TEACHER:
		mine = resolve_scope(persona).get("students") or []
		if not mine:
			return []
		filters["name"] = ["in", mine]
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
def students(search: str = "", limit: int = 50, persona: str = None):
	return {"students": _students(persona, search, limit)}


def _may_see(persona: str, student: str) -> bool:
	if persona in BACK_OFFICE:
		return True
	return student in (resolve_scope(persona).get("students") or [])


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
	context = {
		"filled_on": str(filled)[:16] if filled else "",
		"entry": entry_doc,
		"template": template_doc,
		"fields": fields,
		"values": pretty,
		"school": frappe.db.get_default("company") or "",
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
	return {"html": PRINT_STYLE + body, "title": f"{doc.template_title} — {doc.student_name}"}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def preview_print(template: str, html: str = None, persona: str = None):
	"""What a print design looks like, before it is saved or filled.

	Rendered against the latest filled copy when there is one, and against
	sample answers when there is not, so a designer sees a real page rather
	than a skeleton of empty rows.
	"""
	doc = frappe.get_doc("MS Form Template", template)
	if html is not None:
		doc.print_template = html
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
		return {"html": PRINT_STYLE + _render(doc, entry)}
	except Exception as exc:
		# Jinja errors arrive as HTML with a traceback; a designer needs the
		# sentence, not the page.
		import re as _re

		reason = _re.sub(r"<[^>]+>", " ", str(exc)).strip()
		return fail(
			"The print template could not be rendered.",
			f"خطأ في القالب: {reason[:200]}",
		)
