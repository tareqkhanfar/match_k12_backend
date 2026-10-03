# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""جدول المناوبة — who stands where during the breaks and at the end of the day.

Each rota is a «برنامج»: the first break for grades 1–4, the second break for
5–12, the end of the day, the back yard at the end of the day. For each school
day it names the teachers on duty and, usually, where each one stands
(«خديجة — باب البناية الجديدة»).

It is written by hand and only by hand: nothing in the timetable reads or
changes it, and it changes nothing there. Printing follows the school's paper
— a titled table per rota, the day down the side — and is a print design like
the others (Settings › تصاميم الطباعة › «جدول المناوبة»).
"""

import json

import frappe
from frappe.utils import cint, escape_html

from match_schools.api import forms_print, print_designs
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	fail,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
DOCTYPE = "MS Duty Program"
PRINT_KEY = "duty_schedule"
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]
DAY_AR = {"Sunday": "الأحد", "Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء", "Thursday": "الخميس"}
MAX_ENTRIES = 300


def _people() -> dict[str, str]:
	return {
		r.name: r.instructor_name or r.name
		for r in frappe.get_all("Instructor", filters={"status": ["!=", "Left"]}, fields=["name", "instructor_name"])
	}


def _entries(raw, people: dict[str, str] | None = None) -> list[dict]:
	"""A rota's rows, clean: a known day, a teacher (from the list or typed),
	and where they stand."""
	rows = parse_json_arg(raw) if raw else []
	people = people if people is not None else _people()
	out = []
	for r in (rows if isinstance(rows, list) else [])[:MAX_ENTRIES]:
		if not isinstance(r, dict) or r.get("day") not in DAYS:
			continue
		teacher = r.get("teacher") if r.get("teacher") in people else ""
		name = (str(r.get("name") or "").strip() or people.get(teacher, ""))[:120]
		if not name:
			continue
		out.append({"day": r["day"], "teacher": teacher, "name": name, "place": str(r.get("place") or "").strip()[:120]})
	return out


def _hhmm(value) -> str:
	"""A Time field as 09:30 — it arrives as «9:30:00» or a timedelta."""
	if not value:
		return ""
	parts = str(value).split(":")
	try:
		return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
	except (ValueError, IndexError):
		return ""


def _program(doc, people: dict[str, str]) -> dict:
	return {
		"id": doc.name,
		"title": doc.title,
		"periodLabel": doc.period_label or "",
		"fromTime": _hhmm(doc.from_time),
		"toTime": _hhmm(doc.to_time),
		"academicYear": doc.academic_year,
		"sortOrder": cint(doc.sort_order),
		"isActive": bool(cint(doc.is_active)),
		"notes": doc.notes or "",
		"entries": _entries(doc.entries, people),
	}


def _programs(active_only: bool = False, names: list[str] | None = None) -> list[dict]:
	people = _people()
	year = get_default_academic_year()
	filters: dict = {}
	if active_only:
		filters["is_active"] = 1
	if names:
		filters["name"] = ["in", names]
	rows = frappe.get_all(DOCTYPE, filters=filters, pluck="name", order_by="sort_order asc, creation asc", limit_page_length=0)
	out = [_program(frappe.get_doc(DOCTYPE, n), people) for n in rows]
	# This year's rotas, and any not tied to a year.
	return [p for p in out if not p["academicYear"] or not year or p["academicYear"] == year]


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def list_programs(persona: str = None):
	"""Every rota, the teachers to choose from, the places already used, and how
	many duties each teacher has in a week across the active rotas."""
	programs = _programs()
	load: dict[str, int] = {}
	for p in programs:
		if not p["isActive"]:
			continue
		for e in p["entries"]:
			load[e["name"]] = load.get(e["name"], 0) + 1
	places = sorted({e["place"] for p in programs for e in p["entries"] if e["place"]})
	people = _people()
	return {
		"programs": programs,
		"days": [{"key": d, "label": DAY_AR[d]} for d in DAYS],
		"instructors": [{"id": k, "name": v} for k, v in sorted(people.items(), key=lambda x: x[1])],
		"places": places,
		"load": sorted(({"name": k, "count": v} for k, v in load.items()), key=lambda x: (-x["count"], x["name"])),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def save_program(payload: str | dict = None, persona: str = None):
	data = parse_json_arg(payload) or {}
	title = str(data.get("title") or "").strip()
	if not title:
		return fail(message_en="A title is required.", message_ar="عنوان البرنامج مطلوب (مثال: برنامج المناوبة للعصرونة الأولى).")
	name = data.get("id")
	if name and not frappe.db.exists(DOCTYPE, name):
		return fail(message_en="Not found.", message_ar="البرنامج غير موجود.")
	doc = frappe.get_doc(DOCTYPE, name) if name else frappe.new_doc(DOCTYPE)
	if not name:
		doc.academic_year = get_default_academic_year()
		doc.sort_order = (frappe.db.sql(f"select max(sort_order) from `tab{DOCTYPE}`")[0][0] or 0) + 1
	entries = _entries(data.get("entries"))
	doc.update(
		{
			"title": title[:140],
			"period_label": str(data.get("periodLabel") or "").strip()[:80],
			"from_time": data.get("fromTime") or None,
			"to_time": data.get("toTime") or None,
			"is_active": 1 if cint(data.get("isActive", 1)) else 0,
			"notes": data.get("notes") or "",
			"entries": json.dumps(entries, ensure_ascii=False),
		}
	)
	if "sortOrder" in data:
		doc.sort_order = cint(data.get("sortOrder"))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": _program(doc, _people()),
		"message_en": "Saved.",
		"message_ar": f"تم حفظ «{doc.title}» ({len(entries)} مناوبة).",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def delete_program(program: str = None, persona: str = None):
	if not program or not frappe.db.exists(DOCTYPE, program):
		return fail(message_en="Not found.", message_ar="البرنامج غير موجود.")
	frappe.delete_doc(DOCTYPE, program, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {"success": True, "data": {"id": program}, "message_en": "Deleted.", "message_ar": "تم حذف البرنامج."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def move_program(program: str = None, direction: int = 0, persona: str = None):
	"""Up or down in the printed order."""
	rows = frappe.get_all(DOCTYPE, pluck="name", order_by="sort_order asc, creation asc", limit_page_length=0)
	if program not in rows:
		return fail(message_en="Not found.", message_ar="البرنامج غير موجود.")
	i = rows.index(program)
	j = i + (1 if cint(direction) > 0 else -1)
	if 0 <= j < len(rows):
		rows[i], rows[j] = rows[j], rows[i]
	for n, name in enumerate(rows, start=1):
		frappe.db.set_value(DOCTYPE, name, "sort_order", n, update_modified=False)
	frappe.db.commit()
	return {"success": True, "data": {"order": rows}, "message_en": "Moved.", "message_ar": "تم تغيير الترتيب."}


# --- Printing ---------------------------------------------------------------------------

CSS = """
.ms-banner img { max-height: 30mm; }
.dp { page-break-inside: avoid; break-inside: avoid; margin-bottom: 8mm; }
.dp + .dp { margin-top: 2mm; }
.dp-title { text-align: center; font-size: 19px; font-weight: 800; text-decoration: underline; text-underline-offset: 5px; margin: 2mm 0 4mm; }
.dp-time { text-align: center; font-size: 12px; color: #444; margin: -2mm 0 3mm; }
.dp-time .t { unicode-bidi: isolate; direction: ltr; display: inline-block; }
table.dp-t { width: 100%; border-collapse: collapse; font-size: 15px; }
.dp-t th, .dp-t td { border: 1.2px solid #222; padding: 5px 10px; vertical-align: top; }
.dp-t th { font-size: 16px; font-weight: 800; text-align: center; }
.dp-t td.day { width: 18%; font-weight: 800; text-align: center; vertical-align: middle; }
.dp-t .item { line-height: 1.75; }
.dp-t .place { color: #222; }
.dp-names { text-align: center; }
.dp.paged + .dp.paged { page-break-before: always; break-before: page; }
"""

DESIGN = """{{ banner() }}
{% for p in programs %}
<div class="dp{% if one_per_page %} paged{% endif %}">
  <div class="dp-title">{{ p.title }}</div>
  {% if p.from_time %}<div class="dp-time">{{ p.period_label }} <span class="t">{{ p.from_time }}{% if p.to_time %} – {{ p.to_time }}{% endif %}</span></div>{% endif %}
  <table class="dp-t">
    <thead><tr><th>اليوم</th><th>{{ 'المناوبة' if p.has_places else 'المعلم/ المعلمة' }}</th></tr></thead>
    <tbody>
      {% for d in p.days %}
      <tr>
        <td class="day">{{ d.label }}</td>
        <td>
          {% if p.has_places %}
            {% for e in d.people %}<div class="item">- {{ e.name }}{% if e.place %} <span class="place">( {{ e.place }} )</span>{% endif %}</div>{% endfor %}
          {% else %}
            <div class="dp-names">{{ d.people | map(attribute='name') | join(' – ') }}</div>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endfor %}
"""

PRINT_DEFAULT = {"design": DESIGN, "css": CSS, "theme": "letter", "orientation": "Portrait"}

VARIABLES = [
	("{{ banner() }}", "صورة ترويسة المدرسة (أو اسم المدرسة إن لم توجد صورة)"),
	("{% for p in programs %}", "البرامج بالترتيب: p.title العنوان، p.period_label الفترة، p.from_time · p.to_time الوقت"),
	("p.has_places", "صحيح إن كان لأي مناوب مكان محدّد في هذا البرنامج"),
	("{% for d in p.days %}", "الأيام: d.label الاسم، d.people المناوبون"),
	("e.name · e.place", "اسم المناوب ومكان المناوبة"),
	("{{ one_per_page }}", "صحيح عند اختيار «كل برنامج في صفحة»"),
	("{{ school }} · {{ today }}", "اسم المدرسة، وتاريخ الطباعة"),
]


def render(design, programs: list[dict], one_per_page: bool = False) -> str:
	ctx_programs = []
	for p in programs:
		days = [
			{
				"label": DAY_AR[d],
				"people": [
					{"name": escape_html(e["name"]), "place": escape_html(e["place"])}
					for e in p["entries"]
					if e["day"] == d
				],
			}
			for d in DAYS
		]
		ctx_programs.append(
			{
				"title": escape_html(p["title"]),
				"period_label": escape_html(p.get("periodLabel") or ""),
				"from_time": p.get("fromTime") or "",
				"to_time": p.get("toTime") or "",
				"has_places": any(e["place"] for e in p["entries"]),
				"days": days,
			}
		)
	compiled = forms_print.compile_design(design.design)
	base = print_designs.base_context(design, fallback_class="dp-title")
	page = forms_print.draw(compiled, {**base, "programs": ctx_programs, "one_per_page": one_per_page})
	return forms_print.frame([page], design.theme, design.orientation, design.css, "dp-page")


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def print_programs(programs: str | list = None, one_per_page: int = 0, persona: str = None):
	"""The chosen rotas (every active one when none are chosen), printed in order."""
	wanted = [p for p in (parse_json_arg(programs) or []) if isinstance(p, str)]
	chosen = _programs(active_only=not wanted, names=wanted or None)
	if not chosen:
		return fail(message_en="Nothing to print.", message_ar="لا توجد برامج مناوبة للطباعة.")
	try:
		html = render(print_designs.effective(PRINT_KEY), chosen, bool(cint(one_per_page)))
	except forms_print.DesignError as exc:
		return fail(
			message_en="The print design could not be rendered.",
			message_ar=f"{exc} — راجع «جدول المناوبة» في الإعدادات › تصاميم الطباعة.",
		)
	title = chosen[0]["title"] if len(chosen) == 1 else "جدول المناوبة"
	return {"html": html, "title": title}


def print_sample(design) -> str:
	programs = _programs(active_only=True)
	if programs:
		return render(design, programs)
	sample = [
		("Sunday", "خديجة", "باب البناية الجديدة"), ("Sunday", "سمارة", "بين الطلاب"), ("Sunday", "أسيل", "المقصف الأمامي"),
		("Monday", "شهد", "بين الطلاب"), ("Monday", "حمد", "الباب الأمامي"),
		("Tuesday", "شيرين", "المقصف الأمامي"), ("Tuesday", "فريد", "بين الطلاب"),
		("Wednesday", "ضحى", "بين الطلاب"), ("Wednesday", "إكرام", "المقصف الأمامي"),
		("Thursday", "مناصرة", "الباب الأمامي"), ("Thursday", "رجاء", "بين الطلاب"),
	]
	end = [("Sunday", "عبير"), ("Sunday", "ضحى"), ("Monday", "هبة"), ("Tuesday", "دولين"), ("Wednesday", "سناء"), ("Thursday", "فريد")]
	return render(
		design,
		[
			{
				"title": "برنامج المناوبة للعصرونة الأولى للصفوف من (1-4)",
				"periodLabel": "الاستراحة الأولى",
				"fromTime": "",
				"toTime": "",
				"entries": [{"day": d, "teacher": "", "name": n, "place": pl} for d, n, pl in sample],
			},
			{
				"title": "برنامج المناوبة آخر الدوام",
				"periodLabel": "",
				"fromTime": "",
				"toTime": "",
				"entries": [{"day": d, "teacher": "", "name": n, "place": ""} for d, n in end],
			},
		],
	)
