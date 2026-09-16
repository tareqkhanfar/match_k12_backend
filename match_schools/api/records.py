# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Every field of a student, guardian or teacher, read and edited from the portal.

The profile pages show a chosen handful of fields; the rest were reachable only
from Desk. This reads the DocType's own layout, so a field a school adds later
appears without a portal release, and writes back through `doc.save()` so every
validation the Desk form runs still runs here.
"""

import frappe
from frappe.utils import cint, flt

from match_schools.api.utils import BACK_OFFICE, fail, ms_endpoint, parse_json_arg

DOCTYPES = ("Student", "Guardian", "Instructor")

LAYOUT = ("Section Break", "Column Break", "Tab Break", "Fold")
NOT_SHOWN = ("HTML", "Button", "Heading", "Password", "Signature", "Geolocation", "Image")
EDITABLE = (
	"Data", "Phone", "Int", "Float", "Currency", "Percent", "Date", "Datetime", "Time",
	"Select", "Check", "Link", "Small Text", "Text", "Long Text", "Text Editor",
)
# Changed through their own screens: the account link has a credentials panel,
# the photo has an uploader, and the name comes from its parts.
PROTECTED = {
	"user", "ms_user", "image", "amended_from", "student_name", "employee",
}
HIDDEN = {"naming_series"}

# Labels Frappe ships without an Arabic translation.
AR = {
	"Student Email Address": "البريد الإلكتروني", "Date of Birth": "تاريخ الميلاد",
	"Student Mobile Number": "رقم الجوال", "Nationality": "الجنسية", "Address": "العنوان",
	"Address Line 1": "العنوان (السطر الأول)", "Address Line 2": "العنوان (السطر الثاني)",
	"City": "المدينة", "State": "المنطقة", "Country": "الدولة",
	"Date of Leaving": "تاريخ المغادرة", "Leaving Certificate Number": "رقم شهادة المغادرة",
	"Guardians": "أولياء الأمور", "Siblings": "الإخوة",
	"Studying in Same Institute": "يدرس في نفس المدرسة", "Student ID": "رقم الطالب",
	"Institution": "المؤسسة", "Alternate Number": "رقم بديل", "Occupation": "المهنة",
	"Designation": "المسمى الوظيفي", "Work Address": "عنوان العمل", "Students": "الطلاب",
	"Student Name": "اسم الطالب", "Interest": "الاهتمام", "Instructor Name": "اسم المعلم",
	"User account": "حساب المستخدم", "Status": "الحالة", "Department": "القسم",
	"Instructor Log": "سجل المعلم", "Other details": "تفاصيل أخرى",
	"Active": "نشط", "Left": "غادر", "Guardian Name": "اسم ولي الأمر",
	"Exit": "المغادرة", "Place Of Birth": "مكان الولادة",
	"Mothor ID": "رقم هوية الأم", "Mother ID": "رقم هوية الأم", "Father ID": "رقم هوية الأب",
}


def _check(doctype: str, name: str):
	if doctype not in DOCTYPES:
		frappe.throw(frappe._("This record type cannot be edited here."), frappe.PermissionError)
	if not frappe.db.exists(doctype, name):
		frappe.throw(frappe._("Record not found."), frappe.DoesNotExistError)


def _ar(text: str | None) -> str:
	if not text:
		return ""
	text = text.strip()
	translated = frappe._(text, lang="ar")
	return AR.get(text, translated) if translated == text else translated


def _editable(df, doc) -> bool:
	# A fetched field follows its link; with the link empty there is nothing
	# to follow, and the value is the school's to set.
	fetched = df.fetch_from and doc.get(df.fetch_from.split(".")[0])
	return (
		df.fieldtype in EDITABLE
		and not df.read_only
		and not fetched
		and not getattr(df, "is_virtual", 0)
		and df.fieldname not in PROTECTED
	)


def _value(df, value):
	if df.fieldtype in ("Date", "Datetime", "Time") and value:
		return str(value)
	return value


def _layout(doc) -> dict:
	meta = frappe.get_meta(doc.doctype)
	sections, tables = [], []
	current = {"label": "", "fields": []}
	tab = ""

	for df in meta.fields:
		if df.fieldtype == "Tab Break":
			tab = _ar(df.label)
			if current["fields"]:
				sections.append(current)
			current = {"label": tab, "fields": []}
			continue
		if df.fieldtype == "Section Break":
			if current["fields"]:
				sections.append(current)
			current = {"label": _ar(df.label) or tab, "fields": []}
			continue
		if df.fieldtype in LAYOUT or df.fieldtype in NOT_SHOWN or df.hidden or df.fieldname in HIDDEN:
			continue

		if df.fieldtype in ("Table", "Table MultiSelect"):
			child = frappe.get_meta(df.options)
			columns = [
				c for c in child.fields
				if c.fieldtype not in LAYOUT and c.fieldtype not in NOT_SHOWN and not c.hidden
				and c.fieldtype not in ("Table", "Table MultiSelect")
			][:8]
			tables.append({
				"fieldname": df.fieldname,
				"label": _ar(df.label),
				"columns": [{"fieldname": c.fieldname, "label": _ar(c.label)} for c in columns],
				"rows": [
					{c.fieldname: _value(c, row.get(c.fieldname)) for c in columns}
					for row in doc.get(df.fieldname) or []
				],
			})
			continue

		options = None
		if df.fieldtype == "Select":
			options = [
				{"value": o, "label": _ar(o)} for o in (df.options or "").split("\n") if o != ""
			]
		current["fields"].append({
			"fieldname": df.fieldname,
			"label": _ar(df.label) or df.fieldname,
			"fieldtype": df.fieldtype,
			"options": options if df.fieldtype == "Select" else df.options,
			"reqd": cint(df.reqd),
			"editable": _editable(df, doc),
			"value": _value(df, doc.get(df.fieldname)),
		})

	if current["fields"]:
		sections.append(current)
	return {"doctype": doc.doctype, "name": doc.name, "sections": sections, "tables": tables}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def record_fields(doctype: str, name: str, persona: str = None):
	_check(doctype, name)
	return _layout(frappe.get_doc(doctype, name))


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def save_record_fields(doctype: str, name: str, values=None, persona: str = None):
	_check(doctype, name)
	values = parse_json_arg(values, {}) or {}
	doc = frappe.get_doc(doctype, name)
	meta = frappe.get_meta(doctype)

	for fieldname, value in values.items():
		df = meta.get_field(fieldname)
		if not df or not _editable(df, doc):
			return fail(
				f"The field {fieldname} cannot be edited here.",
				f"لا يمكن تعديل الحقل {_ar(df.label) if df else fieldname} من هنا.",
			)
		if value in ("", None):
			value = 0 if df.fieldtype in ("Check", "Int", "Float", "Currency", "Percent") else None
		elif df.fieldtype in ("Check", "Int"):
			value = cint(value)
		elif df.fieldtype in ("Float", "Currency", "Percent"):
			value = flt(value)
		doc.set(fieldname, value)

	doc.save()
	return _layout(doc)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def link_options(doctype: str, fieldname: str, txt: str = "", persona: str = None):
	"""Choices for one Link field, searched by id and title."""
	if doctype not in DOCTYPES:
		frappe.throw(frappe._("This record type cannot be edited here."), frappe.PermissionError)
	df = frappe.get_meta(doctype).get_field(fieldname)
	if not df or df.fieldtype != "Link" or df.read_only or df.fieldname in PROTECTED:
		return []

	target = frappe.get_meta(df.options)
	title = target.title_field if target.title_field and target.has_field(target.title_field) else None
	fields = ["name"] + ([title] if title else [])
	or_filters = None
	if txt:
		or_filters = [["name", "like", f"%{txt}%"]]
		if title:
			or_filters.append([title, "like", f"%{txt}%"])
	filters = {"disabled": 0} if target.has_field("disabled") else {}

	rows = frappe.get_all(
		df.options, fields=fields, filters=filters, or_filters=or_filters,
		limit=20, order_by="name asc",
	)
	return [
		{"value": r.name, "label": (r.get(title) if title else None) or _ar(r.name)}
		for r in rows
	]
