# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""جدول الإشغال — who is free to cover each period of the week.

When a teacher is away, the office needs, for that day and period, the
colleagues who are not teaching then — and in the order the school has agreed
to call on them. The paper version is one table for the week: a column per
period, and under it the free teachers with how many lessons they already
have that day, fewest first.

It is read from the weekly timetable (MS Timetable Slot) and nothing here
writes to it: generating, saving and printing leave every timetable as it was.
Periods are matched by their number, as the school's paper does — the same
number can run at a different hour in another stage, which the screen notes.

The ordering is the school's rule, set by the administration once and applied
every time (stored as a site default, like the mail policy). A generated table
can be saved, so the copy the staff were given stays fixed even if the
timetable changes afterwards.
"""

import json
import re

import frappe
from frappe.utils import cint, escape_html, get_fullname, now

from match_schools.api import forms_print, print_designs
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	apply_period,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	parse_json_arg,
)

OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
RULE_KEY = "ms_cover_rule"
DOCTYPE = "MS Cover Schedule"
PRINT_KEY = "cover_schedule"

WEEK = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_AR = {
	"Sunday": "الأحد", "Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء",
	"Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت",
}
ORDINALS = ["الأولى", "الثانية", "الثالثة", "الرابعة", "الخامسة", "السادسة", "السابعة", "الثامنة", "التاسعة", "العاشرة"]

# How free teachers can be ordered. The rule is a list of these, most
# important first; the rest only break ties.
SORT_KEYS = {
	"day_load": "الأقل حصصاً في ذلك اليوم",
	"week_load": "الأقل حصصاً في الأسبوع",
	"covers": "الأقل إشغالاً (نيابةً) هذا الفصل",
	"name": "الاسم أبجدياً",
}
DEFAULT_RULE = {
	"order": ["day_load", "week_load", "covers", "name"],
	# A teacher with no lessons that day is not at school.
	"skip_day_off": 1,
	# Only someone already in school and not yet gone: between their first
	# and last lesson of the day.
	"present_only": 0,
	# Leave out anyone with more than this many lessons that day (0: no cap).
	"max_day_load": 0,
	# Names under each period (0: everyone free).
	"max_rows": 0,
	# Never listed — the principal, a coordinator.
	"exclude": [],
	# «full»: the name as recorded; «short»: the first name (two words when
	# two teachers share it), as the school's paper writes them.
	"names": "short",
}

HONORIFIC = re.compile(r"^\s*(?:أ\.?|د\.?|م\.?|الأستاذة?|الاستاذة?|الشيخ|Mr\.?|Mrs\.?|Ms\.?|Dr\.?)\s+", re.I)


# --- The rule ----------------------------------------------------------------------


def _clean_rule(raw) -> dict:
	data = parse_json_arg(raw) if raw else {}
	data = data if isinstance(data, dict) else {}
	order = [k for k in (data.get("order") or []) if k in SORT_KEYS]
	order = list(dict.fromkeys(order)) + [k for k in DEFAULT_RULE["order"] if k not in order]
	exclude = [e for e in (data.get("exclude") or []) if isinstance(e, str)]
	return {
		"order": order,
		"skip_day_off": 1 if cint(data.get("skip_day_off", DEFAULT_RULE["skip_day_off"])) else 0,
		"present_only": 1 if cint(data.get("present_only", 0)) else 0,
		"max_day_load": max(0, min(cint(data.get("max_day_load")), 20)),
		"max_rows": max(0, min(cint(data.get("max_rows")), 60)),
		"exclude": exclude,
		"names": "full" if data.get("names") == "full" else "short",
	}


def current_rule() -> dict:
	return _clean_rule(frappe.db.get_default(RULE_KEY) or "{}")


# --- The week ------------------------------------------------------------------------


def short_names(people: dict[str, str]) -> dict[str, str]:
	"""First names, as the paper writes them; two words where a first name is shared."""
	bare = {k: HONORIFIC.sub("", v or k).strip() or (v or k) for k, v in people.items()}
	# «انس» and «أنس» read as the same name on paper, so they are compared
	# with the alef forms (and taa marbuta) folded together.
	fold = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})
	first = {k: v.split()[0].translate(fold) for k, v in bare.items()}
	counts: dict[str, int] = {}
	for f in first.values():
		counts[f] = counts.get(f, 0) + 1
	return {k: (" ".join(bare[k].split()[:2]) if counts[first[k]] > 1 else bare[k].split()[0]) for k in people}


def _slots() -> list:
	"""This period's weekly lessons, from the sections that are live now."""
	filters: dict = {"disabled": 0}
	apply_period(filters, "Student Group")
	groups = frappe.get_all("Student Group", filters=filters, pluck="name", limit_page_length=0)
	if not groups or not frappe.db.table_exists("MS Timetable Slot"):
		return []
	return frappe.get_all(
		"MS Timetable Slot",
		filters={"active": 1, "student_group": ["in", groups], "instructor": ["is", "set"], "period_order": [">", 0]},
		fields=["day", "period_order", "instructor"],
		limit_page_length=0,
	)


def _covers_this_term() -> dict[str, int]:
	term = get_default_academic_term()
	filters: dict = {"docstatus": 1, "change_type": "Substitute", "instructor": ["is", "set"]}
	if term:
		filters["academic_term"] = term
	counts: dict[str, int] = {}
	for r in frappe.get_all("MS Lesson Change", filters=filters, pluck="instructor", limit_page_length=0):
		counts[r] = counts.get(r, 0) + 1
	return counts


def build(rule: dict) -> dict:
	"""The week's table under `rule` — computed, not stored."""
	slots = _slots()
	busy: dict[str, dict[str, set]] = {}
	for s in slots:
		busy.setdefault(s.instructor, {}).setdefault(s.day, set()).add(cint(s.period_order))

	days = [d for d in WEEK if any(d in per for per in busy.values())]
	periods = sorted({cint(s.period_order) for s in slots})
	people = {
		r.name: r.instructor_name or r.name
		for r in frappe.get_all(
			"Instructor",
			filters={"name": ["in", list(busy) or [""]], "status": ["!=", "Left"]},
			fields=["name", "instructor_name"],
		)
	}
	shown = short_names(people) if rule["names"] == "short" else {k: v for k, v in people.items()}
	covers = _covers_this_term() if "covers" in rule["order"] else {}
	week_load = {t: sum(len(p) for p in per.values()) for t, per in busy.items()}
	excluded = set(rule["exclude"])

	def sort_key(t: str, day_load: int):
		values = {"day_load": day_load, "week_load": week_load.get(t, 0), "covers": covers.get(t, 0), "name": shown.get(t, t)}
		return tuple(values[k] for k in rule["order"])

	out_days = []
	for day in days:
		columns = []
		for p in periods:
			free = []
			for t in people:
				if t in excluded:
					continue
				mine = busy.get(t, {}).get(day, set())
				load = len(mine)
				if p in mine:
					continue
				if rule["skip_day_off"] and not load:
					continue
				if rule["present_only"] and (not mine or not (min(mine) < p < max(mine))):
					continue
				if rule["max_day_load"] and load > rule["max_day_load"]:
					continue
				free.append((sort_key(t, load), t, load))
			free.sort(key=lambda x: x[0])
			if rule["max_rows"]:
				free = free[: rule["max_rows"]]
			columns.append(
				{
					"period": p,
					"teachers": [
						{
							"id": t,
							"name": shown.get(t, t),
							"fullName": people.get(t, t),
							"dayLoad": load,
							"weekLoad": week_load.get(t, 0),
							"covers": covers.get(t, 0),
						}
						for _key, t, load in free
					],
				}
			)
		out_days.append(
			{
				"day": day,
				"label": DAY_AR.get(day, day),
				"periods": columns,
				"rows": max((len(c["teachers"]) for c in columns), default=0),
			}
		)

	return {
		"academicYear": get_default_academic_year(),
		"periods": [{"period": p, "label": ORDINALS[p - 1] if 0 < p <= len(ORDINALS) else str(p)} for p in periods],
		"days": out_days,
		"teachers": len(people),
		"rule": rule,
		"generatedOn": now()[:16],
	}


# --- Endpoints ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def options(persona: str = None):
	"""The rule in force, what it may say, and the saved tables."""
	return {
		"rule": current_rule(),
		"defaultRule": DEFAULT_RULE,
		"sortKeys": [{"key": k, "label": v} for k, v in SORT_KEYS.items()],
		"canEditRule": persona == ROLE_ADMIN,
		"instructors": [
			{"id": r.name, "name": r.instructor_name or r.name}
			for r in frappe.get_all(
				"Instructor", filters={"status": ["!=", "Left"]}, fields=["name", "instructor_name"], order_by="instructor_name"
			)
		],
		"saved": [
			{"id": r.name, "title": r.title, "on": str(r.creation)[:16], "by": get_fullname(r.owner)}
			for r in frappe.get_all(
				DOCTYPE, fields=["name", "title", "creation", "owner"], order_by="creation desc", limit=30
			)
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_rule(rule: str | dict = None, persona: str = None):
	"""The school's rule for ordering free teachers — the administration's call."""
	clean = _clean_rule(rule)
	frappe.db.set_default(RULE_KEY, json.dumps(clean, ensure_ascii=False))
	frappe.db.commit()
	return {"success": True, "data": clean, "message_en": "Rule saved.", "message_ar": "تم حفظ قاعدة الإشغال."}


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def generate(rule: str | dict = None, persona: str = None):
	"""The table under the saved rule — or, for the administration trying a
	rule out before saving it, under a draft one."""
	draft = _clean_rule(rule) if (rule and persona == ROLE_ADMIN) else current_rule()
	return build(draft)


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def save_table(title: str = None, notes: str = None, persona: str = None):
	"""Keep today's table as generated under the saved rule, so the printed copy
	stays what it was even after the timetable moves on."""
	data = build(current_rule())
	if not data["days"]:
		return fail(message_en="Nothing to save.", message_ar="لا يوجد جدول دراسي أسبوعي يُبنى منه جدول الإشغال.")
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"title": (title or "").strip()[:140] or f"جدول الإشغال — {data['generatedOn'][:10]}",
			"academic_year": data["academicYear"],
			"rule": json.dumps(data["rule"], ensure_ascii=False),
			"data": json.dumps(data, ensure_ascii=False),
			"notes": notes or "",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name, "title": doc.title}, "message_en": "Saved.", "message_ar": "تم حفظ جدول الإشغال."}


def _saved(name: str) -> dict | None:
	if not name or not frappe.db.exists(DOCTYPE, name):
		return None
	doc = frappe.get_doc(DOCTYPE, name)
	data = json.loads(doc.data or "{}")
	data["id"] = doc.name
	data["title"] = doc.title
	return data


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def get_table(saved: str = None, persona: str = None):
	data = _saved(saved)
	return data or fail(message_en="Not found.", message_ar="لم يتم العثور على الجدول المحفوظ.")


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*OFFICE)
def delete_table(saved: str = None, persona: str = None):
	if not saved or not frappe.db.exists(DOCTYPE, saved):
		return fail(message_en="Not found.", message_ar="لم يتم العثور على الجدول المحفوظ.")
	frappe.delete_doc(DOCTYPE, saved, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {"success": True, "data": {"id": saved}, "message_en": "Deleted.", "message_ar": "تم حذف الجدول المحفوظ."}


# --- Printing ---------------------------------------------------------------------------

CSS = """
.ms-banner img { max-height: 30mm; }
.cv-title { text-align: center; font-size: 18px; font-weight: 800; margin: 1mm 0 3mm; }
table.cv { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; }
.cv th, .cv td { border: 1px solid #222; padding: 2px 3px; text-align: center; vertical-align: middle; }
.cv thead th { font-size: 13px; font-weight: 800; height: 9mm; }
.cv th.day { width: 7mm; font-size: 13px; font-weight: 800; }
.cv th.day span { display: inline-block; writing-mode: vertical-rl; transform: rotate(180deg); white-space: nowrap; }
.cv th.corner { width: 7mm; font-size: 9px; line-height: 1.2; }
.cv td.n { font-weight: 600; }
.cv td.c { width: 5mm; font-weight: 800; font-size: 10.5px; background: #f2f2f2; }
.cv tbody.dayblock { page-break-inside: avoid; }
.cv tbody.dayblock tr:first-child td, .cv tbody.dayblock tr:first-child th { border-top: 2.2px solid #111; }
.cv thead { display: table-header-group; }
.cv-note { font-size: 10px; color: #444; margin-top: 2mm; }
"""

DESIGN = """{{ banner() }}
<div class="cv-title">جدول الإشغال{% if year %} / {{ year }}{% endif %}</div>
<table class="cv">
  <thead>
    <tr>
      <th class="corner">الحصة<br>اليوم</th>
      {% for p in periods %}<th colspan="2">{{ p.label }}</th>{% endfor %}
    </tr>
  </thead>
  {% for d in days %}
  <tbody class="dayblock">
    {% for i in range(d.rows or 1) %}
    <tr>
      {% if loop.first %}<th class="day" rowspan="{{ d.rows or 1 }}"><span>{{ d.label }}</span></th>{% endif %}
      {% for col in d.periods %}{% set t = col.teachers[i] if i < col.teachers|length else none %}
      <td class="n">{{ t.name if t else '' }}</td><td class="c">{{ t.dayLoad if t else '' }}</td>
      {% endfor %}
    </tr>
    {% endfor %}
  </tbody>
  {% endfor %}
</table>
<div class="cv-note">الرقم بجانب الاسم: عدد حصص المعلم في ذلك اليوم.</div>
"""

PRINT_DEFAULT = {"design": DESIGN, "css": CSS, "theme": "letter", "orientation": "Portrait"}

VARIABLES = [
	("{{ banner() }}", "صورة ترويسة المدرسة (أو اسم المدرسة إن لم توجد صورة)"),
	("{{ year }}", "العام الدراسي: 2026/2027"),
	("{% for p in periods %}", "الحصص: p.label اسمها (الأولى…)، p.period رقمها"),
	("{% for d in days %}", "الأيام: d.label الاسم، d.rows أكبر عدد أسماء تحت حصة، d.periods الأعمدة"),
	("{% for col in d.periods %}", "حصة في ذلك اليوم: col.teachers المعلمون المتاحون بالترتيب"),
	("t.name · t.dayLoad", "اسم المعلم وعدد حصصه في ذلك اليوم (وأيضاً t.weekLoad، t.covers، t.fullName)"),
	("{{ school }} · {{ today }}", "اسم المدرسة، وتاريخ الطباعة"),
]


def render(design, data: dict) -> str:
	year = (data.get("academicYear") or "").replace("-", "/")
	compiled = forms_print.compile_design(design.design)
	base = print_designs.base_context(design, fallback_class="cv-title")
	esc = {
		**data,
		"year": escape_html(year),
		"days": [
			{
				**d,
				"label": escape_html(d["label"]),
				"periods": [
					{**c, "teachers": [{**t, "name": escape_html(t["name"]), "fullName": escape_html(t.get("fullName") or "")} for t in c["teachers"]]}
					for c in d["periods"]
				],
			}
			for d in data.get("days") or []
		],
	}
	page = forms_print.draw(compiled, {**base, **esc})
	return forms_print.frame([page], design.theme, design.orientation, design.css, "cv-page")


@frappe.whitelist()
@ms_endpoint(*OFFICE)
def print_table(saved: str = None, persona: str = None):
	"""A saved table, or the one the saved rule gives now, as a printable page."""
	data = _saved(saved) if saved else build(current_rule())
	if not data:
		return fail(message_en="Not found.", message_ar="لم يتم العثور على الجدول المحفوظ.")
	try:
		html = render(print_designs.effective(PRINT_KEY), data)
	except forms_print.DesignError as exc:
		return fail(
			message_en="The print design could not be rendered.",
			message_ar=f"{exc} — راجع «جدول الإشغال» في الإعدادات › تصاميم الطباعة.",
		)
	return {"html": html, "title": data.get("title") or "جدول الإشغال"}


def print_sample(design) -> str:
	"""The table the saved rule gives now, or a made-up one on a site with no
	weekly timetable yet."""
	data = build(current_rule())
	if data["days"]:
		return render(design, data)
	names = ["حمد", "جمال", "شيرين", "نورس", "دولين", "رابعة", "عويسات", "ضحى", "رجاء", "ماهر"]
	periods = [{"period": i + 1, "label": ORDINALS[i]} for i in range(7)]
	days = []
	for di, day in enumerate(["Sunday", "Monday"]):
		cols = []
		for p in range(7):
			rot = names[(p + di) % len(names):] + names[: (p + di) % len(names)]
			cols.append({"period": p + 1, "teachers": [{"name": n, "dayLoad": 3 + (j // 3), "fullName": n} for j, n in enumerate(rot[:6])]})
		days.append({"day": day, "label": DAY_AR[day], "periods": cols, "rows": 6})
	return render(design, {"academicYear": get_default_academic_year() or "2026-2027", "periods": periods, "days": days})
