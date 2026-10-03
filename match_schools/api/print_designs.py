# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""تصاميم الطباعة — how the system's printed pages look, set from Settings.

Some pages print from here (the admission letter, the transfer certificate,
the periodic report) and every table in the portal prints from the browser.
Each ships with a design that works on any site. The school's administration
may replace any of them with its own — the same Jinja/HTML design, CSS, page
theme and orientation the forms designer uses, plus the letterhead image —
and go back to the default at any time.

Which design a page prints with:

  1. the one saved here (MS Print Design, named by the page's key);
  2. for a page that is a form, the school's own form of the same title
     (MS Form Template, category «school») — how such a page was customised
     before this existed, so what a school did there keeps applying;
  3. the built-in design.

The letterhead (`banner()` in a design) is the design's own image when it
names one, else the image the school's letter-style forms already print with
— the school's paper letterhead.

Adding a page is one entry in DOCUMENTS (where its built-in design lives and
how to draw a sample of it for the editor), then printing it through
`effective(key)`.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, get_fullname, nowdate

from match_schools.api import admission_request_form, forms, forms_print, transfer_certificate
from match_schools.api.document_theme import school_name
from match_schools.api.utils import BACK_OFFICE, ROLE_ADMIN, fail, ms_endpoint, parse_json_arg

DOCTYPE = "MS Print Design"
SOURCES = {"saved": "مخصَّص", "school": "نموذج المدرسة", "builtin": "الافتراضي"}

# What a design written against a form can call (forms_print.Page).
FORM_VARIABLES = [
	("{{ banner() }}", "الترويسة: صورة ترويسة المدرسة بعرض الصفحة"),
	('{{ field("اسم الحقل") }}', "قيمة الحقل باسمه، أو خط منقّط إن كان فارغاً"),
	("{{ values.fieldname }}", "قيمة الحقل بمعرّفه (فارغة إن لم يُعبّأ) — المعرّفات في قائمة الحقول"),
	('{{ info("عنوان", values.fieldname) }}', "سطر «عنوان: قيمة» مع خط منقّط إن كانت فارغة"),
	('{{ inline("اسم الحقل") }}', "سطر «اسم الحقل: القيمة»"),
	('{{ checks("اسم الحقل") }}', "خيارات الحقل كمربعات ☐ والمختار معلَّم"),
	('{{ chosen("اسم الحقل") }}', "الخيارات المختارة كقائمة — للاستخدام في {% for %}"),
	("{{ filled_date }}", "تاريخ الطباعة"),
	("{{ school }}", "اسم المدرسة"),
	("{{ lines(3) }}", "أسطر منقّطة للكتابة باليد"),
	("{{ letterhead() }}", "ترويسة مرسومة (الشعار واسم المدرسة) بدل الصورة"),
]

PERIODIC_VARIABLES = [
	("{{ banner() }}", "صورة ترويسة المدرسة (أو اسم المدرسة إن لم توجد صورة)"),
	("{{ heading_line }}", "العنوان مع الفترة: «التقييم الشهري للطالب لشهر (2)»"),
	("{{ heading }} · {{ period }}", "عنوان النموذج وحده، والفترة وحدها"),
	("{{ year_label }}", "العام الدراسي: «(2025م – 2026م)»"),
	("{{ student.name }}", "اسم الطالب"),
	("{{ grade }} · {{ section }}", "الصف (بلا كلمة «الصف») والشعبة"),
	("{% for s in subjects %}", "المواد بترتيب الصف: s.name الاسم، s.answers الإجابات، s.by من قيّم"),
	("{% for c in subject_criteria %}", "معايير المواد: c.label العنوان، c.key المعرّف، c.type النوع"),
	("{{ answer(c, s.answers.get(c.key)) }}", "إجابة معيار: خيارات المقياس وحول المختار دائرة"),
	("{% for c in homeroom_criteria %}", "معايير مربي الصف، وإجاباتها في homeroom"),
	("{{ answer(c, homeroom.get(c.key), false) }}", "إجابة مربي الصف كما هي، أو خط منقّط"),
	("{{ homeroom_teacher }}", "اسم مربي الصف"),
	("{{ principal }}", "اسم مدير المدرسة كما في النموذج"),
	("{{ scale }}", "خيارات المقياس (ممتاز، جيد جدا…)"),
	("{{ school }} · {{ today }}", "اسم المدرسة، وتاريخ الطباعة"),
]

TABLE_VARIABLES = [
	("{{ banner() }}", "صورة ترويسة المدرسة (أو اسم المدرسة إن لم توجد صورة)"),
	("{{ title }}", "عنوان الجدول أو الصفحة التي طُبع منها"),
	("{{ table }}", "الجدول نفسه — الصفوف الظاهرة بعد الفلترة، بصنف tp-table"),
	("{{ count }}", "عدد الصفوف المطبوعة"),
	("{{ date }}", "تاريخ الطباعة"),
	("{{ school }}", "اسم المدرسة"),
	("{{ user_name }}", "اسم من يطبع"),
]

# Every page whose print the school may design. Paths are dotted so a page's
# own module can import this one without a cycle.
DOCUMENTS: dict[str, dict] = {
	"admission_request": {
		"title": "خطاب المتسع",
		"page": "طلبات المتسع",
		"description": "خطاب قبول الطالب (متسع) الذي يُطبع للطلب المؤكَّد.",
		"school_title": admission_request_form.TITLE,
		"default": "match_schools.api.admission_request_form.PRINT_DEFAULT",
		"sample": "match_schools.api.admission_requests.print_sample",
		"fields": "match_schools.api.admission_requests.print_fields",
		"variables": FORM_VARIABLES,
	},
	"student_transfer": {
		"title": "شهادة انتقال الطالب",
		"page": "الطلاب المنقولون",
		"description": "شهادة الانتقال الرسمية التي تُطبع لطلب نقل الطالب إلى مدرسة أخرى.",
		"school_title": transfer_certificate.TITLE,
		# A school's earlier copy of the certificate opens with the ministry
		# lines typed out; it gets the letterhead image in their place.
		"adapt": "match_schools.api.transfer_certificate.with_letterhead",
		"default": "match_schools.api.transfer_certificate.PRINT_DEFAULT",
		"sample": "match_schools.api.transfers.print_sample",
		"fields": "match_schools.api.transfers.print_fields",
		"variables": [
			*FORM_VARIABLES,
			("{{ student.name }} · {{ student.date_of_birth }}", "بيانات الطالب من سجله: name, date_of_birth, guardian, program, city…"),
		],
	},
	"periodic_report": {
		"title": "تقرير التقييم الدوري",
		"page": "نماذج التقييم الدورية",
		"description": "صفحة لكل طالب: تقييم المواد، خانة مربي الصف، والتواقيع والخاتم.",
		"default": "match_schools.api.periodic_evaluations.PRINT_DEFAULT",
		"sample": "match_schools.api.periodic_evaluations.print_sample",
		"variables": PERIODIC_VARIABLES,
		"note": "النموذج الدوري الذي حُدّدت له ترويسة خاصة في إعداداته يُطبع بها بدل ترويسة هذا التصميم.",
	},
	"table": {
		"title": "طباعة الجداول",
		"page": "كل الجداول (زر «طباعة» فوق الجدول)",
		"description": "الإطار الذي يُطبع فيه أي جدول في النظام: الترويسة والعنوان وتاريخ الطباعة والتذييل.",
		"default": "match_schools.api.print_designs.TABLE_DEFAULT",
		"sample": "match_schools.api.print_designs.table_sample",
		"variables": TABLE_VARIABLES,
		"note": "إن تعذّر تحميل هذا التصميم يُطبع الجدول بالشكل البسيط المعتاد.",
	},
}


# --- The table frame -----------------------------------------------------------

TABLE_CSS = """
.ms-banner img { max-height: 30mm; }
.tp-head { text-align: center; margin: 0 0 4mm; }
.tp-title { font-size: 17px; font-weight: 800; margin: 0; }
.tp-meta { font-size: 11px; color: #555; margin-top: 1mm; }
table.tp-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.tp-table th, .tp-table td { border: 1px solid #999; padding: 4px 7px; text-align: right; vertical-align: top; }
.tp-table th { background: #eef1f7; font-weight: 700; }
.tp-table tr { page-break-inside: avoid; }
.tp-table thead { display: table-header-group; }
.tp-foot { display: flex; justify-content: space-between; gap: 8mm; margin-top: 4mm; padding-top: 1.5mm; border-top: 1px solid #bbb; font-size: 10.5px; color: #555; }
"""
TABLE_DESIGN = """{{ banner() }}
<div class="tp-head">
  <h2 class="tp-title">{{ title }}</h2>
  <div class="tp-meta">تاريخ الطباعة: {{ date }} · {{ count }} صف</div>
</div>
{{ table }}
<div class="tp-foot"><span>{{ school }}</span><span>طبعه: {{ user_name }}</span></div>
"""
TABLE_DEFAULT = {"design": TABLE_DESIGN, "css": TABLE_CSS, "theme": "classic", "orientation": "Landscape"}

# The browser fills these in: the frame is drawn here once and reused for
# every table, so the rows themselves never travel to the server.
TABLE_TOKENS = {"title": "%%MS_TITLE%%", "date": "%%MS_DATE%%", "count": "%%MS_COUNT%%", "table": "%%MS_TABLE%%"}


# --- Which design a page prints with ---------------------------------------------


def _entry(key: str | None) -> dict:
	entry = DOCUMENTS.get(key or "")
	if not entry:
		frappe.throw(_("Unknown printed page."), frappe.ValidationError)
	return entry


def _unknown(key: str | None):
	"""The refusal for a page not in DOCUMENTS, in both languages."""
	if key in DOCUMENTS:
		return None
	return fail("Unknown printed page.", "هذه الصفحة ليست من تصاميم الطباعة.")


def default_letterhead() -> str:
	"""The school's letterhead image: the one its letter-style forms print with."""
	rows = frappe.get_all(
		"MS Form Template",
		filters={"print_logo": ["is", "set"], "print_theme": "letter"},
		pluck="print_logo",
		order_by="modified desc",
		limit=1,
	)
	return rows[0] if rows else ""


def _school_form(entry: dict):
	title = entry.get("school_title")
	if not title:
		return None
	name = frappe.db.get_value("MS Form Template", {"title": title, "category": "school"}, "name")
	return frappe.get_doc("MS Form Template", name) if name else None


def builtin(key: str) -> frappe._dict:
	"""The design the page ships with."""
	d = frappe.get_attr(_entry(key)["default"])
	return frappe._dict(
		design=d["design"], css=d["css"], theme=d["theme"], orientation=d["orientation"], logo="", source="builtin"
	)


def fallback(key: str) -> frappe._dict:
	"""What the page prints with when nothing is saved here: the school's own
	form of the same title if it has one, else the built-in design."""
	entry = _entry(key)
	school = _school_form(entry)
	if not school:
		return builtin(key)
	design = school.print_template or forms_print.design_from_fields(
		forms._field_rows(school), school.get("entry_for") or "Student"
	)
	if entry.get("adapt"):
		design = frappe.get_attr(entry["adapt"])(design)
	# A form whose image is the school's letterhead simply uses the letterhead
	# — shown as such in the editor, and following it if the school changes it.
	logo = school.print_logo or ""
	if logo == default_letterhead():
		logo = ""
	return frappe._dict(
		design=design,
		css=school.print_css or "",
		theme=school.print_theme or "soft",
		orientation=school.print_orientation or "Portrait",
		logo=logo,
		source="school",
		school_form=school.name,
	)


def effective(key: str) -> frappe._dict:
	"""The design `key` prints with now — {design, css, theme, orientation,
	logo, letterhead, source} — saved here, else the school's form, else built in."""
	_entry(key)
	row = frappe.db.get_value(
		DOCTYPE,
		key,
		["print_template", "print_css", "print_theme", "print_orientation", "print_logo", "modified", "modified_by"],
		as_dict=True,
	)
	if row:
		d = frappe._dict(
			design=row.print_template or "",
			css=row.print_css or "",
			theme=row.print_theme if row.print_theme in forms_print.THEMES else "classic",
			orientation=row.print_orientation or "Portrait",
			logo=row.print_logo or "",
			source="saved",
			modified=str(row.modified)[:16],
			modified_by=get_fullname(row.modified_by),
		)
	else:
		d = fallback(key)
	d.letterhead = d.logo or default_letterhead()
	return d


def _clean_logo(url) -> str:
	"""Only an image stored on this site: the page carries it inline, so it
	prints anywhere and nothing is fetched from elsewhere."""
	url = str(url or "").strip()
	return url if url.startswith(("/files/", "/private/files/")) else ""


def draft(key: str, data: dict) -> frappe._dict:
	"""An unsaved design from the editor, shaped like `effective`."""
	_entry(key)
	logo = _clean_logo(data.get("logo"))
	return frappe._dict(
		design=str(data.get("design") or ""),
		css=str(data.get("css") or ""),
		theme=data.get("theme") if data.get("theme") in forms_print.THEMES else "classic",
		orientation="Landscape" if data.get("orientation") == "Landscape" else "Portrait",
		logo=logo,
		letterhead=logo or default_letterhead(),
		source="draft",
	)


# --- Drawing ----------------------------------------------------------------------


def apply_to_form(template_doc, design: frappe._dict):
	"""Put a design on a form (in memory only) so forms_print draws the page
	with it; the form keeps its own fields."""
	template_doc.print_template = design.design
	template_doc.print_css = design.css
	template_doc.print_theme = design.theme
	template_doc.print_orientation = design.orientation
	template_doc.print_logo = design.letterhead
	return template_doc


def base_context(design: frappe._dict, fallback_class: str = "tp-school") -> dict:
	"""What every design that is not a form can use."""
	school = school_name()
	head = forms_print.banner_html(design.letterhead) or (
		f'<div class="{fallback_class}">{escape_html(school)}</div>'
	)
	return {
		"banner": lambda: head,
		"school": escape_html(school),
		"today": forms_print._fmt_date(nowdate()),
		"user_name": escape_html(get_fullname(frappe.session.user)),
	}


def table_html(design: frappe._dict, title: str, date: str, count, table: str) -> str:
	ctx = {**base_context(design), "title": title, "date": date, "count": count, "table": table}
	page = forms_print.draw(forms_print.compile_design(design.design), ctx)
	return forms_print.frame([page], design.theme, design.orientation, design.css, "tp-page")


def table_sample(design: frappe._dict) -> str:
	"""A made-up table for the editor, as the browser would hand it over."""
	rows = [
		("1", "أحمد محمد الخطيب", "السابع", "أ", "حاضر"),
		("2", "سارة خالد يوسف", "السابع", "أ", "حاضرة"),
		("3", "محمود علي حسن", "السابع", "ب", "غائب"),
		("4", "ليان سامر عودة", "السابع", "ب", "حاضرة"),
		("5", "يوسف إبراهيم سالم", "الثامن", "أ", "متأخر"),
		("6", "نور عماد داود", "الثامن", "أ", "حاضرة"),
	]
	head = "".join(f"<th>{h}</th>" for h in ("#", "الطالب", "الصف", "الشعبة", "الحضور"))
	body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
	table = f'<table class="tp-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
	return table_html(design, "سجل الحضور (مثال)", forms_print._fmt_date(nowdate()), len(rows), table)


def _sample(key: str, design: frappe._dict) -> tuple[str | None, str | None]:
	"""The page drawn with `design` on a sample — (html, None) or (None, why not)."""
	try:
		forms_print.compile_design(design.design)  # a syntax error, with its line
		return frappe.get_attr(_entry(key)["sample"])(design), None
	except Exception as exc:
		# A failed render leaves desk markup in the message log; the editor
		# shows the sentence instead.
		frappe.clear_messages()
		return None, forms_print.error_text(exc)


# --- Endpoints --------------------------------------------------------------------


def _payload(key: str) -> dict:
	entry = _entry(key)
	d = effective(key)
	fields = frappe.get_attr(entry["fields"])() if entry.get("fields") else []
	has_school_form = bool(_school_form(entry))
	# With a school form in the way, the built-in design is one click away.
	shipped = builtin(key) if has_school_form else None
	return {
		"document": key,
		"title": entry["title"],
		"page": entry["page"],
		"description": entry["description"],
		"note": entry.get("note") or "",
		"source": d.source,
		"sourceLabel": SOURCES[d.source],
		"schoolForm": entry.get("school_title") if has_school_form else "",
		"design": d.design,
		"css": d.css,
		"theme": d.theme,
		"orientation": d.orientation,
		"logo": d.logo,
		"defaultLogo": default_letterhead(),
		"modified": d.get("modified") or "",
		"modifiedBy": d.get("modified_by") or "",
		"variables": [{"code": c, "label": label} for c, label in entry.get("variables") or []],
		"fields": [
			{"fieldname": f["fieldname"], "label": f["label"], "fieldtype": f["fieldtype"]}
			for f in fields
			if f["fieldtype"] not in forms_print.LAYOUT
		],
		"builtin": (
			{"design": shipped.design, "css": shipped.css, "theme": shipped.theme, "orientation": shipped.orientation}
			if shipped
			else None
		),
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def list_designs(persona: str = None):
	"""Every page whose print can be designed, and what each prints with now."""
	saved = {
		r.name: r for r in frappe.get_all(DOCTYPE, fields=["name", "modified", "modified_by"], limit_page_length=0)
	}
	rows = []
	for key, entry in DOCUMENTS.items():
		mine = saved.get(key)
		source = "saved" if mine else ("school" if _school_form(entry) else "builtin")
		rows.append(
			{
				"document": key,
				"title": entry["title"],
				"page": entry["page"],
				"description": entry["description"],
				"source": source,
				"sourceLabel": SOURCES[source],
				"modified": str(mine.modified)[:16] if mine else "",
				"modifiedBy": get_fullname(mine.modified_by) if mine else "",
			}
		)
	return {"documents": rows, "canEdit": persona == ROLE_ADMIN}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def get_design(document: str = None, persona: str = None):
	"""The design a page prints with now — what the editor opens on."""
	return _unknown(document) or _payload(document)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def preview(document: str = None, payload: str | dict = None, persona: str = None):
	"""An unsaved design drawn on a sample — the latest real record when there
	is one, made-up details otherwise."""
	if refusal := _unknown(document):
		return refusal
	html, error = _sample(document, draft(document, parse_json_arg(payload) or {}))
	if error:
		return fail("The print design could not be rendered.", error)
	return {"html": html}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_design(document: str = None, payload: str | dict = None, persona: str = None):
	"""Make a design the one the page prints with. One that cannot be drawn is
	refused, so a typo never reaches the office's printer."""
	if refusal := _unknown(document):
		return refusal
	entry = _entry(document)
	d = draft(document, parse_json_arg(payload) or {})
	if not d.design.strip():
		return fail("The design is empty.", "تصميم الطباعة فارغ — اكتبه، أو استعد التصميم الافتراضي.")
	_, error = _sample(document, d)
	if error:
		return fail("The print design could not be rendered.", f"لم يُحفظ التصميم — {error}")
	if frappe.db.exists(DOCTYPE, document):
		doc = frappe.get_doc(DOCTYPE, document)
	else:
		doc = frappe.new_doc(DOCTYPE)
		doc.document = document
	doc.update(
		{
			"title": entry["title"],
			"print_template": d.design,
			"print_css": d.css,
			"print_theme": d.theme,
			"print_orientation": d.orientation,
			"print_logo": d.logo or None,
		}
	)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": _payload(document),
		"message_en": "Saved.",
		"message_ar": f"تم حفظ تصميم «{entry['title']}» — يُطبع به من الآن.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def reset_design(document: str = None, persona: str = None):
	"""Forget the design saved here: the page goes back to its default (the
	school's own form when it has one, else the built-in design)."""
	if refusal := _unknown(document):
		return refusal
	entry = _entry(document)
	if frappe.db.exists(DOCTYPE, document):
		frappe.delete_doc(DOCTYPE, document, ignore_permissions=True, force=True)
		frappe.db.commit()
	data = _payload(document)
	where = f"نموذج المدرسة «{data['schoolForm']}»" if data["source"] == "school" else "التصميم الافتراضي"
	return {
		"success": True,
		"data": data,
		"message_en": "Restored.",
		"message_ar": f"عاد «{entry['title']}» إلى {where}.",
	}


@frappe.whitelist()
@ms_endpoint()
def table_frame(persona: str = None):
	"""The page every printed table sits in, with places for the browser to
	fill: the title, the date, the row count and the table itself."""
	try:
		html = table_html(effective("table"), **TABLE_TOKENS)
	except forms_print.DesignError as exc:
		return fail("The table print design could not be rendered.", str(exc))
	return {"html": html, "tokens": TABLE_TOKENS}
