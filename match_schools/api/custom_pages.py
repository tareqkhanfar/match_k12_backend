"""الصفحات — a page built from a form, without code.

A school sends twelve counselling forms and wants each on its own page: a list
of what was filled, a button to fill a new one, printing. Writing a screen per
form per school does not scale, so a page is a record instead: which form it
shows, who may open it, and per field the rules a developer would otherwise
hard-code —

- hidden / read only / required on this page,
- a default value,
- **fetch**: where the value comes from — the student's record (date of birth,
  guardian, section…), the section, the person filling, today's date, or the
  latest answer in another form about the same student. «always» keeps it
  locked to its source; «if empty» fills it once and lets the user change it.

The copies are ordinary `MS Form Entry` rows, so everything that already reads
forms — the student file's «النماذج» tab, the forms screens, reports — sees
them too. Permissions are the forms engine's own; the page only narrows them.

Building pages is the Administrator's alone (the user's rule, as for the help
centre); using them follows the roles the page names.
"""

import datetime
import json
import re

import frappe
from frappe.utils import cint, get_fullname, getdate, now_datetime, nowdate

from match_schools.api import forms
from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

PAGE_PERSONAS = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
DEFAULT_PERSONAS = [ROLE_ADMIN, ROLE_SECRETARY]

# Columns every page can show besides the form's own answers.
BUILTIN_COLUMNS = {
	"subject": "الطالب / الشعبة",
	"section": "الشعبة",
	"status": "الحالة",
	"filledBy": "عبّأه",
	"filledOn": "تاريخ التعبئة",
}
# Answers worth a column: short ones. A long text or a table is read on the copy.
COLUMN_TYPES = ("Data", "Number", "Date", "Time", "Datetime", "Select", "Multi Select", "Checkbox", "Rating")
# Answers worth a filter: a closed set of values.
FILTER_TYPES = ("Select", "Multi Select", "Checkbox", "Rating")


# --- who -----------------------------------------------------------------------


def is_administrator() -> bool:
	return frappe.session.user == "Administrator"


def assert_administrator():
	if not is_administrator():
		frappe.throw(
			"بناء مساحات العمل والصفحات مقصور على حساب Administrator.",
			frappe.PermissionError,
		)


def raw(fn):
	"""The function under its decorators — to call another endpoint's logic
	without its envelope or its persona check being applied twice."""
	while hasattr(fn, "__wrapped__"):
		fn = fn.__wrapped__
	return fn


def _json(value, default):
	if not value:
		return default
	if isinstance(value, (list, dict)):
		return value
	try:
		return json.loads(value)
	except Exception:
		return default


def page_personas(doc) -> list[str]:
	chosen = [p for p in _json(doc.get("personas"), []) if p in PAGE_PERSONAS]
	return chosen or list(DEFAULT_PERSONAS)


def may_open(doc, persona: str) -> bool:
	if is_administrator():
		return True
	return cint(doc.is_active) and persona in page_personas(doc)


# --- fetch sources -----------------------------------------------------------------

# key → (label, group, what it needs). "student" sources need a student;
# "section" ones the student's section or the form's section; "context" ones
# nothing at all.
SOURCES = {
	"student.name": ("اسم الطالب الكامل", "الطالب", "student"),
	"student.first_name": ("الاسم الأول", "الطالب", "student"),
	"student.id": ("رقم الطالب في النظام", "الطالب", "student"),
	"student.id_number": ("رقم هوية الطالب", "الطالب", "student"),
	"student.date_of_birth": ("تاريخ الميلاد", "الطالب", "student"),
	"student.age": ("العمر (بالسنوات)", "الطالب", "student"),
	"student.gender": ("الجنس", "الطالب", "student"),
	"student.nationality": ("الجنسية", "الطالب", "student"),
	"student.blood_group": ("فصيلة الدم", "الطالب", "student"),
	"student.mobile": ("هاتف الطالب", "الطالب", "student"),
	"student.father_mobile": ("جوال الأب", "الطالب", "student"),
	"student.mother_mobile": ("جوال الأم", "الطالب", "student"),
	"student.email": ("بريد الطالب", "الطالب", "student"),
	"student.address": ("العنوان", "الطالب", "student"),
	"student.joining_date": ("تاريخ الالتحاق", "الطالب", "student"),
	"student.program": ("الصف", "الطالب", "student"),
	"guardian.name": ("اسم ولي الأمر", "ولي الأمر", "student"),
	"guardian.relation": ("صلة القرابة", "ولي الأمر", "student"),
	"guardian.mobile": ("هاتف ولي الأمر", "ولي الأمر", "student"),
	"guardian.email": ("بريد ولي الأمر", "ولي الأمر", "student"),
	"guardian.occupation": ("مهنة ولي الأمر", "ولي الأمر", "student"),
	"guardian.id_number": ("رقم هوية ولي الأمر", "ولي الأمر", "student"),
	"section.name": ("الشعبة", "الشعبة", "section"),
	"section.program": ("صف الشعبة", "الشعبة", "section"),
	"section.homeroom": ("مربي الصف", "الشعبة", "section"),
	"section.count": ("عدد طلاب الشعبة", "الشعبة", "section"),
	"user.name": ("اسم من يعبّئ النموذج", "التعبئة", "context"),
	"context.today": ("تاريخ اليوم", "التعبئة", "context"),
	"context.now": ("التاريخ والوقت الآن", "التعبئة", "context"),
	"context.time": ("الوقت الآن", "التعبئة", "context"),
	"context.weekday": ("اليوم (الأحد، الإثنين…)", "التعبئة", "context"),
	"context.academic_year": ("العام الدراسي", "المدرسة", "context"),
	"context.academic_term": ("الفصل الدراسي", "المدرسة", "context"),
	"context.school": ("اسم المدرسة", "المدرسة", "context"),
}
# «from another form»: `form:<template>:<fieldname>` — the latest answer to that
# field in a copy of that form about the same student (or section).
FORM_SOURCE = re.compile(r"^form:([^:]+):(.+)$")

WEEKDAYS = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def source_needs(source: str) -> str | None:
	if source in SOURCES:
		return SOURCES[source][2]
	if FORM_SOURCE.match(source or ""):
		return "subject"
	return None


def follows_subject(source: str) -> bool:
	"""Whether the value belongs to the student/section — recomputed when the
	subject changes — rather than to the moment of filling."""
	return source_needs(source) in ("student", "section", "subject")


def _student_record(student: str) -> dict:
	return (
		frappe.db.get_value(
			"Student",
			student,
			[
				"name", "student_name", "first_name", "ms_id_number", "date_of_birth", "gender",
				"nationality", "blood_group", "student_mobile_number", "student_email_id",
				"address_line_1", "city", "joining_date", "ms_father_mobile", "ms_mother_mobile",
			],
			as_dict=True,
		)
		or {}
	)


def _guardian(student: str) -> dict:
	row = frappe.get_all(
		"Student Guardian",
		filters={"parent": student, "parenttype": "Student"},
		fields=["guardian", "guardian_name", "relation"],
		order_by="idx",
		limit=1,
	)
	if not row:
		return {}
	out = {"name": row[0].guardian_name or "", "relation": row[0].relation or ""}
	if row[0].guardian:
		g = frappe.db.get_value(
			"Guardian",
			row[0].guardian,
			["guardian_name", "mobile_number", "email_address", "occupation", "ms_id_number"],
			as_dict=True,
		) or {}
		out["name"] = out["name"] or g.get("guardian_name") or ""
		out["mobile"] = g.get("mobile_number") or ""
		out["email"] = g.get("email_address") or ""
		out["occupation"] = g.get("occupation") or ""
		out["id_number"] = g.get("ms_id_number") or ""
	return out


def current_section(student: str) -> str | None:
	"""The student's section this year — the latest active one, preferring the
	current academic year."""
	year = get_default_academic_year()
	rows = frappe.db.sql(
		"""SELECT sg.name, sg.academic_year
		     FROM `tabStudent Group Student` sgs
		     JOIN `tabStudent Group` sg ON sg.name = sgs.parent
		    WHERE sgs.student = %s AND sgs.parenttype = 'Student Group' AND sgs.active = 1
		      AND IFNULL(sg.disabled, 0) = 0
		    ORDER BY sg.modified DESC""",
		student,
		as_dict=True,
	)
	for r in rows:
		if not year or r.academic_year == year:
			return r.name
	return rows[0].name if rows else None


def _section(group: str) -> dict:
	g = frappe.db.get_value(
		"Student Group", group, ["student_group_name", "program", "ms_homeroom_instructor"], as_dict=True
	) or {}
	homeroom = (
		frappe.db.get_value("Instructor", g.get("ms_homeroom_instructor"), "instructor_name")
		if g.get("ms_homeroom_instructor")
		else ""
	)
	count = frappe.db.count(
		"Student Group Student", {"parent": group, "parenttype": "Student Group", "active": 1}
	)
	return {
		"name": g.get("student_group_name") or group,
		"program": g.get("program") or "",
		"homeroom": homeroom or "",
		"count": str(count),
	}


def _age(dob) -> str:
	if not dob:
		return ""
	born = getdate(dob)
	today = getdate(nowdate())
	years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
	return str(max(years, 0))


def _gender(value: str) -> str:
	return {"Male": "ذكر", "Female": "أنثى"}.get(value or "", value or "")


def _program_of(student: str, section: str | None) -> str:
	if section:
		program = frappe.db.get_value("Student Group", section, "program")
		if program:
			return program
	row = frappe.get_all(
		"Program Enrollment",
		filters={"student": student, "docstatus": 1},
		fields=["program"],
		order_by="enrollment_date desc",
		limit=1,
	)
	return row[0].program if row else ""


def _latest_answer(template: str, fieldname: str, student: str | None, section: str | None, skip: str | None):
	filters = {"template": template}
	if student:
		filters["student"] = student
	elif section:
		filters["student_group"] = section
	else:
		return ""
	if skip:
		filters["name"] = ["!=", skip]
	entries = frappe.get_all("MS Form Entry", filters=filters, pluck="name", order_by="modified desc", limit=1)
	if not entries:
		return ""
	return (
		frappe.db.get_value(
			"MS Form Value", {"parent": entries[0], "parenttype": "MS Form Entry", "fieldname": fieldname}, "value"
		)
		or ""
	)


def resolve_sources(
	sources: set[str], student: str | None = None, section: str | None = None, skip_entry: str | None = None
) -> dict[str, str]:
	"""Every requested source's value for this student / section, as text."""
	out: dict[str, str] = {}
	if not sources:
		return out
	if student and not section and any(s.startswith("section.") for s in sources):
		section = current_section(student)
	st = _student_record(student) if student and any(s.startswith("student.") for s in sources) else {}
	gu = _guardian(student) if student and any(s.startswith("guardian.") for s in sources) else {}
	sec = _section(section) if section and any(s.startswith("section.") for s in sources) else {}
	now = now_datetime()
	for s in sources:
		value = ""
		if s.startswith("student.") and student:
			key = s.split(".", 1)[1]
			value = {
				"name": st.get("student_name"),
				"first_name": st.get("first_name"),
				"id": st.get("name"),
				"id_number": st.get("ms_id_number"),
				"date_of_birth": str(st.get("date_of_birth") or ""),
				"age": _age(st.get("date_of_birth")),
				"gender": _gender(st.get("gender")),
				"nationality": st.get("nationality"),
				"blood_group": st.get("blood_group"),
				"mobile": st.get("student_mobile_number"),
				"father_mobile": st.get("ms_father_mobile"),
				"mother_mobile": st.get("ms_mother_mobile"),
				"email": st.get("student_email_id"),
				"address": "، ".join(x for x in (st.get("address_line_1"), st.get("city")) if x),
				"joining_date": str(st.get("joining_date") or ""),
				"program": _program_of(student, section) if key == "program" else "",
			}.get(key)
		elif s.startswith("guardian.") and student:
			value = gu.get(s.split(".", 1)[1])
		elif s.startswith("section.") and section:
			value = sec.get(s.split(".", 1)[1])
		elif s == "user.name":
			value = get_fullname(frappe.session.user)
		elif s == "context.today":
			value = nowdate()
		elif s == "context.now":
			value = now.strftime("%Y-%m-%d %H:%M")
		elif s == "context.time":
			value = now.strftime("%H:%M")
		elif s == "context.weekday":
			value = WEEKDAYS[now.weekday()]
		elif s == "context.academic_year":
			value = get_default_academic_year()
		elif s == "context.academic_term":
			value = get_default_academic_term()
		elif s == "context.school":
			value = frappe.db.get_default("company")
		else:
			m = FORM_SOURCE.match(s)
			if m:
				value = _latest_answer(m[1], m[2], student, section, skip_entry)
		out[s] = "" if value is None else str(value)
	return out


# --- the page's settings -------------------------------------------------------------

_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def _norm(text: str) -> str:
	return re.sub(r"[^\w\s]", " ", (text or "").translate(_ALEF)).strip()


# A form's label → the source a person would have typed it from. Checked in
# order, so the specific ones ("اسم ولي الامر") come before the general
# ("الاسم").
_GUESSES = [
	(("هويه ولي", "رقم هويه الاب", "هويه الاب"), "guardian.id_number"),
	(("اسم ولي", "اسم الاب", "ولي الامر"), "guardian.name"),
	(("هاتف ولي", "جوال ولي", "هاتف الاب", "جوال الاب", "رقم ولي"), "guardian.mobile"),
	(("صله القرابه",), "guardian.relation"),
	(("مهنه ولي", "مهنه الاب", "عمل الاب"), "guardian.occupation"),
	(("رقم الهويه", "هويه الطالب"), "student.id_number"),
	(("تاريخ الميلاد",), "student.date_of_birth"),
	(("العمر",), "student.age"),
	(("الجنسيه",), "student.nationality"),
	(("الجنس",), "student.gender"),
	(("فصيله الدم",), "student.blood_group"),
	(("تاريخ الالتحاق",), "student.joining_date"),
	(("العنوان", "مكان السكن"), "student.address"),
	(("مربي الصف", "مربيه الصف"), "section.homeroom"),
	(("الشعبه",), "section.name"),
	(("الصف",), "student.program"),
	(("اسم الطالب", "اسم الطالبه"), "student.name"),
	(("اسم المرشد", "اسم المرشده", "اسم المعلم", "اسم المعبي", "معبي النموذج"), "user.name"),
	(("العام الدراسي",), "context.academic_year"),
	(("الفصل الدراسي",), "context.academic_term"),
	(("اسم المدرسه",), "context.school"),
	(("اليوم",), "context.weekday"),
	(("التاريخ",), "context.today"),
]


def guess_source(label: str, fieldtype: str, entry_for: str) -> str | None:
	if fieldtype not in ("Data", "Date", "Number", "Datetime", "Time", "Select"):
		return None
	text = _norm(label)
	for needles, source in _GUESSES:
		if any(n in text for n in needles):
			needs = SOURCES[source][2]
			if needs == "student" and entry_for != "Student":
				return None
			if needs == "section" and entry_for == "General":
				return None
			if fieldtype == "Date" and source not in ("student.date_of_birth", "student.joining_date", "context.today"):
				return None
			return source
	return None


def default_config(template_doc) -> dict:
	"""What a new page starts with: sensible columns and filters, and the
	fields that obviously come from the record already set to fetch."""
	entry_for = template_doc.get("entry_for") or "Student"
	value_fields = [f for f in template_doc.fields_table if f.fieldtype not in forms.NO_VALUE]
	columns = ["subject"] if entry_for != "General" else []
	if entry_for == "Student":
		columns.append("section")
	columns += [f.fieldname for f in value_fields if f.fieldtype in COLUMN_TYPES][:3]
	columns += ["status", "filledOn", "filledBy"]
	filters = [f.fieldname for f in value_fields if f.fieldtype in ("Select", "Checkbox")][:3]
	fields = {}
	for f in value_fields:
		source = guess_source(f.label, f.fieldtype, entry_for)
		if source:
			fields[f.fieldname] = {"fetch": {"source": source, "mode": "if_empty"}}
	students = (
		"any"
		if template_doc.category in forms.OPEN_CATEGORIES or not template_doc.get("subjects_table")
		else "subjects"
	)
	return {"fields": fields, "columns": columns, "filters": filters, "students": students, "showNotes": 1}


def normalize_config(data: dict, template_doc) -> dict:
	"""Keep what refers to something real; drop the rest quietly — a field
	removed from the form takes its page rules with it."""
	data = data if isinstance(data, dict) else {}
	by_name = {f.fieldname: f for f in template_doc.fields_table if f.fieldtype not in forms.NO_VALUE}
	fields = {}
	for fieldname, rule in (data.get("fields") or {}).items():
		if fieldname not in by_name or not isinstance(rule, dict):
			continue
		clean = {}
		label = str(rule.get("label") or "").strip()[:140]
		if label:
			clean["label"] = label
		for flag in ("hidden", "readOnly", "reqd"):
			if cint(rule.get(flag)):
				clean[flag] = 1
		if rule.get("width") in ("half", "full", "third"):
			clean["width"] = rule["width"]
		default = str(rule.get("default") or "")[:500]
		if default:
			clean["default"] = default
		fetch = rule.get("fetch") or {}
		source = str(fetch.get("source") or "")
		if source and source_needs(source):
			m = FORM_SOURCE.match(source)
			if not m or frappe.db.exists("MS Form Template", m[1]):
				clean["fetch"] = {
					"source": source,
					"mode": "always" if fetch.get("mode") == "always" else "if_empty",
				}
		if clean:
			fields[fieldname] = clean
	columns = []
	for c in data.get("columns") or []:
		if (c in BUILTIN_COLUMNS or (c in by_name and by_name[c].fieldtype in COLUMN_TYPES)) and c not in columns:
			columns.append(c)
	filters = []
	for c in data.get("filters") or []:
		if c in by_name and by_name[c].fieldtype in FILTER_TYPES + ("Data",) and c not in filters:
			filters.append(c)
	return {
		"fields": fields,
		"columns": columns[:12] or ["subject", "status", "filledOn"],
		"filters": filters[:6],
		"students": "subjects" if data.get("students") == "subjects" else "any",
		"showNotes": 1 if cint(data.get("showNotes", 1)) else 0,
		"newLabel": str(data.get("newLabel") or "").strip()[:60],
	}


def page_config(doc, template_doc=None) -> dict:
	template_doc = template_doc or frappe.get_doc("MS Form Template", doc.template)
	return normalize_config(_json(doc.get("config"), {}), template_doc)


# --- catalogue (builder) ------------------------------------------------------------------


def _source_rows() -> list[dict]:
	return [
		{"key": k, "label": v[0], "group": v[1], "needs": v[2]} for k, v in SOURCES.items()
	]


def _page_row(doc) -> dict:
	title = frappe.db.get_value("MS Form Template", doc.template, ["title", "category"], as_dict=True) or {}
	return {
		"id": doc.name,
		"title": doc.title,
		"icon": doc.icon or "",
		"color": doc.color or "",
		"description": doc.description or "",
		"isActive": cint(doc.is_active),
		"template": doc.template,
		"templateTitle": title.get("title") or "",
		"category": title.get("category") or "",
		"categoryLabel": forms.CATEGORIES.get(title.get("category") or "", ""),
		"personas": page_personas(doc),
		"entries": frappe.db.count("MS Form Entry", {"template": doc.template}) if doc.template else 0,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def list_pages(persona: str = None):
	assert_administrator()
	docs = frappe.get_all(
		"MS Custom Page",
		fields=["name", "title", "icon", "color", "description", "is_active", "template", "personas"],
		order_by="title asc",
		limit_page_length=0,
	)
	return {"pages": [_page_row(d) for d in docs]}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def builder_options(persona: str = None):
	"""What a page can be built from: every form, and every source a field can
	fetch from."""
	assert_administrator()
	templates = frappe.get_all(
		"MS Form Template",
		fields=["name", "title", "category", "entry_for", "is_active"],
		order_by="category asc, title asc",
		limit_page_length=0,
	)
	pages_by_template = {}
	for p in frappe.get_all("MS Custom Page", fields=["name", "template"], limit_page_length=0):
		pages_by_template.setdefault(p.template, []).append(p.name)
	return {
		"templates": [
			{
				"name": t.name,
				"title": t.title,
				"category": t.category,
				"categoryLabel": forms.CATEGORIES.get(t.category, t.category),
				"entryFor": t.entry_for or "Student",
				"isActive": cint(t.is_active),
				"pages": pages_by_template.get(t.name, []),
			}
			for t in templates
		],
		"categories": [{"key": k, "label": v} for k, v in forms.CATEGORIES.items()],
		"sources": _source_rows(),
		"builtinColumns": [{"key": k, "label": v} for k, v in BUILTIN_COLUMNS.items()],
		"fieldTypes": forms.FIELD_TYPES,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def template_fields(template: str, persona: str = None):
	"""Another form's answerable fields — for «من نموذج آخر»."""
	assert_administrator()
	doc = frappe.get_doc("MS Form Template", template)
	return {
		"template": doc.name,
		"title": doc.title,
		"fields": [
			{"fieldname": f.fieldname, "label": f.label, "fieldtype": f.fieldtype}
			for f in doc.fields_table
			if f.fieldtype not in forms.NO_VALUE
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def suggest_config(template: str, persona: str = None):
	"""A fresh page's settings for this form, before anything is saved."""
	assert_administrator()
	return {"config": default_config(frappe.get_doc("MS Form Template", template))}


def _definition(doc, persona: str) -> dict:
	template = frappe.get_doc("MS Form Template", doc.template)
	cfg = page_config(doc, template)
	category = template.category
	may_fill = persona in BACK_OFFICE or forms._teachers_may_fill(category)
	return {
		**_page_row(doc),
		"config": cfg,
		"form": forms._template_payload(template),
		"entryFor": template.get("entry_for") or "Student",
		"mayFill": bool(may_fill) and cint(template.is_active) == 1,
		"mayDelete": persona in BACK_OFFICE,
		"canBuild": is_administrator(),
		"sourceLabels": {k: v[0] for k, v in SOURCES.items()},
	}


def _page(page: str, persona: str):
	if not page or not frappe.db.exists("MS Custom Page", page):
		frappe.throw("الصفحة غير موجودة.", frappe.DoesNotExistError)
	doc = frappe.get_doc("MS Custom Page", page)
	if not may_open(doc, persona):
		frappe.throw("هذه الصفحة غير متاحة لك.", frappe.PermissionError)
	if not doc.template or not frappe.db.exists("MS Form Template", doc.template):
		frappe.throw("نموذج هذه الصفحة حُذف — اختر نموذجاً آخر من إعدادات الصفحة.", frappe.ValidationError)
	return doc


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def get_page(page: str, persona: str = None):
	return _definition(_page(page, persona), persona)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_page(payload: str | dict, persona: str = None):
	assert_administrator()
	data = parse_json_arg(payload) or {}
	title = str(data.get("title") or "").strip()
	if not title:
		return fail("Give the page a title.", "اكتب عنوان الصفحة.")
	template = data.get("template")
	if not template or not frappe.db.exists("MS Form Template", template):
		return fail("Choose the form.", "اختر النموذج الذي تُبنى منه الصفحة.")
	template_doc = frappe.get_doc("MS Form Template", template)
	doc = (
		frappe.get_doc("MS Custom Page", data["id"])
		if data.get("id") and frappe.db.exists("MS Custom Page", data["id"])
		else frappe.new_doc("MS Custom Page")
	)
	doc.title = title[:140]
	doc.page_type = "form"
	doc.template = template
	doc.icon = str(data.get("icon") or "")[:40]
	doc.color = str(data.get("color") or "")[:20]
	doc.description = str(data.get("description") or "")[:500]
	doc.is_active = 1 if cint(data.get("isActive", 1)) else 0
	personas = [p for p in (data.get("personas") or []) if p in PAGE_PERSONAS]
	doc.personas = json.dumps(personas or DEFAULT_PERSONAS)
	config = data.get("config")
	cfg = normalize_config(config, template_doc) if isinstance(config, dict) else default_config(template_doc)
	doc.config = json.dumps(cfg, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _definition(doc, persona)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def delete_page(page: str, persona: str = None):
	"""The page goes; the copies filled through it stay with their form."""
	assert_administrator()
	frappe.delete_doc("MS Custom Page", page, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": page}


def create_page_for(template_doc, title: str | None = None) -> str:
	"""A page for this form with the suggested settings — used when a whole
	file's forms are turned into a workspace at once."""
	doc = frappe.new_doc("MS Custom Page")
	doc.title = (title or template_doc.title)[:140]
	doc.page_type = "form"
	doc.template = template_doc.name
	doc.is_active = 1
	doc.personas = json.dumps(
		DEFAULT_PERSONAS + ([ROLE_TEACHER] if forms._teachers_may_fill(template_doc.category) else [])
	)
	doc.config = json.dumps(default_config(template_doc), ensure_ascii=False)
	doc.insert(ignore_permissions=True)
	return doc.name


# --- using a page ----------------------------------------------------------------------


def _subject_allowed(persona: str, student: str | None, section: str | None):
	if student and not forms._may_see(persona, student):
		frappe.throw("هذا الطالب ليس من طلابك.", frappe.PermissionError)
	if section and persona == ROLE_TEACHER and section not in set(forms._teacher_groups(persona)):
		frappe.throw("هذه الشعبة ليست من شعبك.", frappe.PermissionError)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def students(page: str, search: str = "", limit: int = 30, persona: str = None):
	"""Who this page's form may be filled for."""
	doc = _page(page, persona)
	cfg = page_config(doc)
	template = None if cfg["students"] == "any" else doc.template
	rows = forms._students(persona, search, limit, template)
	out = {"students": rows}
	if template:
		out["subjectsCount"] = len(forms._subjects(template))
	return out


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def fetch(page: str, student: str = None, section: str = None, entry: str = None, persona: str = None):
	"""Values for the fields that fetch, for this student or section."""
	doc = _page(page, persona)
	_subject_allowed(persona, student, section)
	cfg = page_config(doc)
	rules = {f: r["fetch"] for f, r in cfg["fields"].items() if r.get("fetch")}
	values = resolve_sources({r["source"] for r in rules.values()}, student, section, entry)
	return {
		"values": {f: values.get(r["source"], "") for f, r in rules.items()},
		"section": (student and current_section(student)) or section or None,
	}


def _apply_rules(doc, template_doc, data: dict, persona: str) -> dict | None:
	"""Make the copy obey the page: locked fetches from their source, hidden
	and read-only fields from their defaults. Returns a failure envelope when
	a field the page requires is empty."""
	cfg = page_config(doc, template_doc)
	values = dict(data.get("values") or {})
	new = not data.get("name")
	stored = {}
	if not new:
		existing = frappe.get_doc("MS Form Entry", data["name"])
		if existing.template != template_doc.name:
			frappe.throw("هذه النسخة ليست من نموذج هذه الصفحة.", frappe.PermissionError)
		stored = {v.fieldname: v.value or "" for v in existing.values_table}
		if "student" not in data and existing.student:
			data["student"] = existing.student
	student = data.get("student") or None
	section = data.get("studentGroup") or None
	if student and not section:
		section = current_section(student)
		data["studentGroup"] = section
	rules = cfg["fields"]
	sources = {r["fetch"]["source"] for r in rules.values() if r.get("fetch")}
	computed = resolve_sources(sources, student, section, data.get("name"))
	defaults = {f.fieldname: f.default_value or "" for f in template_doc.fields_table}
	for fieldname, rule in rules.items():
		fetch_rule = rule.get("fetch")
		source = fetch_rule["source"] if fetch_rule else None
		if fetch_rule and fetch_rule["mode"] == "always":
			if follows_subject(source) or new:
				values[fieldname] = computed.get(source, "")
			else:
				values[fieldname] = stored.get(fieldname, "")
		elif rule.get("readOnly") or rule.get("hidden"):
			if new:
				given = "" if rule.get("readOnly") else str(values.get(fieldname) or "")
				values[fieldname] = (
					given or (computed.get(source, "") if source else "") or rule.get("default") or defaults.get(fieldname, "")
				)
			else:
				values[fieldname] = stored.get(fieldname, "")
		elif new and source and not str(values.get(fieldname) or "").strip():
			values[fieldname] = computed.get(source, "")
	if data.get("status") == "مكتمل":
		labels = {f.fieldname: f.label for f in template_doc.fields_table}
		missing = [
			rule.get("label") or labels.get(fieldname, fieldname)
			for fieldname, rule in rules.items()
			if rule.get("reqd") and not str(values.get(fieldname) or "").strip()
		]
		if missing:
			return fail("Required fields are empty.", "حقول مطلوبة فارغة: " + "، ".join(missing[:6]))
	data["values"] = values
	data["template"] = template_doc.name
	return None


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def save_entry(page: str, payload: str | dict, persona: str = None):
	doc = _page(page, persona)
	template_doc = frappe.get_doc("MS Form Template", doc.template)
	data = parse_json_arg(payload) or {}
	if not isinstance(data, dict):
		return fail("Nothing to save.", "لا شيء للحفظ.")
	if isinstance(data.get("values"), str):
		data["values"] = parse_json_arg(data["values"]) or {}
	failure = _apply_rules(doc, template_doc, data, persona)
	if failure:
		return failure
	# A page open to any student files for any student the user may see —
	# the forms engine would otherwise insist on the form's own list.
	cfg = page_config(doc, template_doc)
	previous = frappe.flags.get("ms_form_any_student")
	frappe.flags.ms_form_any_student = cfg["students"] == "any"
	try:
		return raw(forms.save_entry)(payload=data, persona=persona)
	finally:
		frappe.flags.ms_form_any_student = previous


def _shown(fieldtype: str, value: str) -> str:
	if value in (None, ""):
		return ""
	if fieldtype == "Checkbox":
		return "نعم" if cint(value) else "لا"
	if fieldtype == "Rating":
		return f"{cint(value)} / 5"
	return str(value)


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE, ROLE_TEACHER)
def entries(
	page: str,
	status: str = None,
	section: str = None,
	date_from: str = None,
	date_to: str = None,
	search: str = None,
	mine: int = 0,
	filters: str | dict = None,
	limit: int = 500,
	persona: str = None,
):
	"""The copies filled of this page's form, with the columns the page shows.

	Filtered on the server: a counselling file grows by the hundred a year,
	and a filter that only sees the first page of it answers the wrong
	question."""
	doc = _page(page, persona)
	template_doc = frappe.get_doc("MS Form Template", doc.template)
	cfg = page_config(doc, template_doc)
	by_name = {f.fieldname: f for f in template_doc.fields_table}

	base = {"template": template_doc.name}
	if section:
		base["student_group"] = section
	if date_from and date_to:
		base["filled_on"] = ["between", [f"{date_from} 00:00:00", f"{date_to} 23:59:59"]]
	elif date_from:
		base["filled_on"] = [">=", f"{date_from} 00:00:00"]
	elif date_to:
		base["filled_on"] = ["<=", f"{date_to} 23:59:59"]
	if cint(mine):
		base["filled_by"] = frappe.session.user

	# Answers: each filter narrows the copies to those whose answer matches.
	wanted = parse_json_arg(filters, {}) or {}
	allowed = None
	for fieldname, value in (wanted.items() if isinstance(wanted, dict) else []):
		if fieldname not in cfg["filters"] or value in (None, "", []):
			continue
		ft = by_name[fieldname].fieldtype if fieldname in by_name else "Data"
		values = value if isinstance(value, list) else [value]
		matched = set()
		for v in values:
			v = str(v)
			if ft in ("Multi Select", "Data"):
				condition, arg = "value LIKE %s", f"%{v}%"
			else:
				condition, arg = "value = %s", v
			matched |= set(
				frappe.db.sql_list(
					f"""SELECT parent FROM `tabMS Form Value`
					     WHERE parenttype = 'MS Form Entry' AND fieldname = %s AND {condition}""",
					(fieldname, arg),
				)
			)
		allowed = matched if allowed is None else (allowed & matched)
	if allowed is not None:
		base["name"] = ["in", sorted(allowed) or [""]]

	or_filters = None
	if persona == ROLE_TEACHER:
		or_filters = [
			["student", "in", forms._teacher_students(persona) or [""]],
			["student_group", "in", forms._teacher_groups(persona) or [""]],
			["filled_by", "=", frappe.session.user],
		]
	if search:
		# A teacher's scope already uses or_filters; the search joins it as a
		# name filter instead so the two do not widen each other.
		hits = frappe.get_all(
			"MS Form Entry",
			filters={"template": template_doc.name},
			or_filters=[["student_name", "like", f"%{search}%"], ["name", "like", f"%{search}%"]],
			pluck="name",
			limit_page_length=0,
		)
		hits_set = set(hits)
		if allowed is not None:
			hits_set &= allowed
		base["name"] = ["in", sorted(hits_set) or [""]]

	# Counted here rather than with COUNT(): the aggregate syntax differs
	# between Frappe 16 releases, and a file's copies are hundreds, not millions.
	counts = {"مسودة": 0, "مكتمل": 0}
	for s in frappe.get_all(
		"MS Form Entry", filters=base, or_filters=or_filters, pluck="status", limit_page_length=0
	):
		counts[s or "مسودة"] = counts.get(s or "مسودة", 0) + 1

	filters_rows = dict(base)
	if status:
		filters_rows["status"] = status
	rows = frappe.get_all(
		"MS Form Entry",
		filters=filters_rows,
		or_filters=or_filters,
		fields=[
			"name", "student", "student_name", "student_group", "status", "filled_by", "filled_on", "modified",
		],
		order_by="filled_on desc, modified desc",
		limit=min(cint(limit) or 500, 2000),
	)
	names = [r.name for r in rows]
	value_columns = [c for c in cfg["columns"] if c not in BUILTIN_COLUMNS]
	answers: dict[str, dict] = {}
	if names and value_columns:
		for v in frappe.get_all(
			"MS Form Value",
			filters={"parent": ["in", names], "parenttype": "MS Form Entry", "fieldname": ["in", value_columns]},
			fields=["parent", "fieldname", "value"],
			limit_page_length=0,
		):
			ft = by_name[v.fieldname].fieldtype if v.fieldname in by_name else "Data"
			answers.setdefault(v.parent, {})[v.fieldname] = _shown(ft, v.value)
	groups = {r.student_group for r in rows if r.student_group}
	labels = dict(
		frappe.get_all(
			"Student Group",
			filters={"name": ["in", list(groups) or [""]]},
			fields=["name", "student_group_name"],
			as_list=True,
		)
	)
	people = {u: get_fullname(u) for u in {r.filled_by for r in rows if r.filled_by}}
	return {
		"rows": [
			{
				"name": r.name,
				"student": r.student,
				"studentName": r.student_name or "",
				"studentGroup": r.student_group or "",
				"sectionLabel": labels.get(r.student_group) or r.student_group or "",
				"status": r.status or "مسودة",
				"filledBy": r.filled_by or "",
				"filledByName": people.get(r.filled_by, r.filled_by or ""),
				"filledOn": str(r.filled_on or "")[:16],
				"values": answers.get(r.name, {}),
			}
			for r in rows
		],
		"counts": counts,
		"total": sum(counts.values()),
	}
