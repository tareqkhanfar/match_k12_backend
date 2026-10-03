# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""الخطة الأسبوعية — a section's week as the school prints it.

The paper the office hands round is one sheet per section: the subjects down
the side, Sunday to Thursday across, and in each square what that subject
covers that day. Teachers already write exactly that — the preparation they
hang on each lesson in the timetable — so the sheet is drawn from it rather
than typed again. A square with no preparation stays empty, a box to write in
by hand, as on the paper.

Its look is a print design like the others (Settings › تصاميم الطباعة,
«الخطة الأسبوعية»), drawn landscape with the school letterhead by default.
"""

import datetime
import html
import re

import frappe
from frappe.utils import escape_html, getdate, nowdate, strip_html_tags

from match_schools.api import forms_print, print_designs
from match_schools.api.periodic_evaluations import _heading_line, _section_courses
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	instructor_groups,
	ms_endpoint,
	resolve_scope,
)

PRINT_KEY = "weekly_plan"
DAYS = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس"]

CSS = """
.ms-banner img { max-height: 34mm; }
.wp-head { text-align: center; margin: 1mm 0 3mm; }
.wp-title { font-size: 19px; font-weight: 800; }
.wp-title small { font-size: 14px; font-weight: 700; }
.wp-dates { margin-top: 1.5mm; font-size: 15px; font-weight: 700; }
.wp-dates .dt { unicode-bidi: isolate; direction: ltr; display: inline-block; }
.wp-dates .sep { margin: 0 4mm; letter-spacing: 1px; }
table.wp { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11.5px; }
.wp th, .wp td { border: 1.4px solid #222; vertical-align: top; padding: 4px 6px; }
.wp thead th { font-size: 14px; font-weight: 800; text-align: center; vertical-align: middle; height: 11mm; }
.wp thead th small { display: block; font-size: 10px; font-weight: 600; color: #444; }
.wp th.subj { width: 15%; font-size: 13.5px; font-weight: 800; text-align: center; vertical-align: middle; }
.wp td { height: 15mm; line-height: 1.5; }
.wp tr { page-break-inside: avoid; }
.wp thead { display: table-header-group; }
.wp .corner { position: relative; padding: 0; }
.wp .corner svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.wp .corner .d { position: absolute; top: 1.5mm; left: 2mm; font-size: 12px; }
.wp .corner .s { position: absolute; bottom: 1.5mm; right: 2mm; font-size: 12px; }
.wp .plan + .plan { border-top: 1px dashed #999; margin-top: 2px; padding-top: 2px; }
.wp .plan b { display: block; font-weight: 800; }
.wp .plan .hw { color: #333; }
.wp .plan .hw i { font-style: normal; font-weight: 700; }
"""

DESIGN = """{{ banner() }}
<div class="wp-head">
  <div class="wp-title">الخطة الأسبوعية {{ for_grade }}{% if section %} <small>— شعبة ({{ section }})</small>{% endif %}</div>
  <div class="wp-dates">
    (<span class="dt">{{ from_date }}</span> - <span class="dt">{{ to_date }}</span>)
    <span class="sep">-----</span>
    (<span class="dt">{{ hijri_from or '1448/ / ' }}</span> - <span class="dt">{{ hijri_to or '1448/ / ' }}</span>هـ)
  </div>
</div>
<table class="wp">
  <thead>
    <tr>
      <th class="subj corner">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none"><line x1="0" y1="100" x2="100" y2="0" stroke="#222" stroke-width="1.2" vector-effect="non-scaling-stroke"/></svg>
        <span class="d">اليوم</span><span class="s">المبحث</span>
      </th>
      {% for d in days %}<th>{{ d.name }}<small>{{ d.date }}</small></th>{% endfor %}
    </tr>
  </thead>
  <tbody>
    {% for s in subjects %}
    <tr>
      <th class="subj">{{ s.name }}</th>
      {% for cell in s.cells %}
      <td>{% for p in cell %}<div class="plan">{% if p.title %}<b>{{ p.title }}</b>{% endif %}{{ p.content or p.objectives }}{% if p.homework %}<div class="hw"><i>الواجب:</i> {{ p.homework }}</div>{% endif %}</div>{% endfor %}</td>
      {% endfor %}
    </tr>
    {% endfor %}
  </tbody>
</table>
"""

PRINT_DEFAULT = {"design": DESIGN, "css": CSS, "theme": "classic", "orientation": "Landscape"}

VARIABLES = [
	("{{ banner() }}", "صورة ترويسة المدرسة (أو اسم المدرسة إن لم توجد صورة)"),
	("{{ for_grade }}", "«للصف العاشر» — الصف مع لام الجر"),
	("{{ grade }} · {{ section }}", "اسم الصف، وحرف الشعبة"),
	("{{ from_date }} · {{ to_date }}", "أول الأسبوع وآخره بالميلادي: 2026/10/04"),
	("{{ hijri_from }} · {{ hijri_to }}", "أول الأسبوع وآخره بالهجري (أم القرى، من المتصفح) — فارغان في المعاينة"),
	("{% for d in days %}", "أيام الأسبوع: d.name الاسم، d.date التاريخ"),
	("{% for s in subjects %}", "المواد بترتيب الصف: s.name الاسم، s.cells خانة لكل يوم"),
	("{% for p in cell %}", "تحضيرات الخانة: p.title الموضوع، p.objectives الأهداف، p.content المحتوى، p.homework الواجب، p.teacher المعلم"),
	("{{ school }} · {{ today }}", "اسم المدرسة، وتاريخ الطباعة"),
]


# --- The week -----------------------------------------------------------------


def week_days(week_start: str | None) -> list[datetime.date]:
	"""Sunday to Thursday of the week holding `week_start` (today by default)."""
	day = getdate(week_start or nowdate())
	sunday = day - datetime.timedelta(days=(day.weekday() + 1) % 7)
	return [sunday + datetime.timedelta(days=i) for i in range(5)]


def _gregorian(d: datetime.date) -> str:
	return f"{d.year}/{d.month:02d}/{d.day:02d}"


def _hijri_arg(value: str | None) -> str:
	value = (value or "").strip()
	return value if re.fullmatch(r"1[34]\d\d/\d{1,2}/\d{1,2}", value) else ""


def _plain(value: str | None) -> str:
	"""A Text Editor field as escaped text with its line breaks kept."""
	if not value:
		return ""
	text = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d)\s*/?>", "\n", value)
	text = re.sub(r"(?i)<li[^>]*>", "• ", text)
	text = html.unescape(strip_html_tags(text))
	lines = [ln.strip() for ln in text.replace("\xa0", " ").splitlines()]
	return "<br>".join(escape_html(ln) for ln in lines if ln)


# --- Drawing ----------------------------------------------------------------------


def _context(group: dict, days: list[datetime.date], subjects: list[dict], hijri: tuple[str, str]) -> dict:
	program = group.get("program") or ""
	return {
		"grade": escape_html(program),
		"for_grade": escape_html(_heading_line("", program).strip()) if program else "",
		"section": escape_html(group.get("batch") or ""),
		"from_date": _gregorian(days[0]),
		"to_date": _gregorian(days[-1]),
		"hijri_from": hijri[0],
		"hijri_to": hijri[1],
		"days": [{"name": DAYS[i], "date": f"{d.month:02d}/{d.day:02d}"} for i, d in enumerate(days)],
		"subjects": subjects,
	}


def render(design, group: dict, days: list, subjects: list[dict], hijri: tuple[str, str]) -> str:
	compiled = forms_print.compile_design(design.design)
	base = print_designs.base_context(design, fallback_class="wp-head")
	page = forms_print.draw(compiled, {**base, **_context(group, days, subjects, hijri)})
	return forms_print.frame([page], design.theme, design.orientation, design.css, "wp-page")


def _subjects(group: str, program: str | None, days: list, only_instructor: str | None, only_courses=None) -> list[dict]:
	"""The section's subjects, each with five squares of preparation."""
	filters = {
		"student_group": group,
		"schedule_date": ["between", [days[0], days[-1]]],
	}
	if only_instructor:
		filters["instructor"] = only_instructor
	plans = frappe.get_all(
		"MS Lesson Plan",
		filters=filters,
		fields=["name", "course", "schedule_date", "title", "objectives", "content", "homework", "instructor", "course_schedule"],
		order_by="schedule_date, creation",
		limit_page_length=0,
	)
	times = {
		r.name: str(r.from_time or "")
		for r in frappe.get_all(
			"Course Schedule",
			filters={"name": ["in", [p.course_schedule for p in plans if p.course_schedule] or [""]]},
			fields=["name", "from_time"],
		)
	}
	plans.sort(key=lambda p: (str(p.schedule_date), times.get(p.course_schedule, ""), p.name))
	teachers = {
		r.name: r.instructor_name or r.name
		for r in frappe.get_all(
			"Instructor",
			filters={"name": ["in", list({p.instructor for p in plans if p.instructor}) or [""]]},
			fields=["name", "instructor_name"],
		)
	}

	courses = _section_courses(group, program, {p.course for p in plans if p.course})
	if only_courses is not None:
		courses = [c for c in courses if c["id"] in only_courses]
	index = {str(d): i for i, d in enumerate(days)}
	cells: dict[str, list[list[dict]]] = {c["id"]: [[] for _ in days] for c in courses}
	for p in plans:
		at = index.get(str(p.schedule_date))
		if at is None or p.course not in cells:
			continue
		item = {
			"title": escape_html((p.title or "").strip()),
			"objectives": _plain(p.objectives),
			"content": _plain(p.content),
			"homework": _plain(p.homework),
			"teacher": escape_html(teachers.get(p.instructor, "")),
		}
		# Two periods of the same subject prepared as one lesson print once.
		if item not in cells[p.course][at]:
			cells[p.course][at].append(item)
	return [{"id": c["id"], "name": escape_html(c["name"]), "cells": cells[c["id"]]} for c in courses]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def weekly_plan(
	student_group: str = None,
	week_start: str = None,
	hijri_from: str = None,
	hijri_to: str = None,
	persona: str = None,
):
	"""One section's week as a printable page.

	The office prints the whole section. A teacher prints the subjects they
	teach there with their own preparation only — reading a colleague's
	preparation is not something the lesson screen offers either.
	"""
	if not student_group or not frappe.db.exists("Student Group", student_group):
		return fail(message_en="Choose a section.", message_ar="اختر الشعبة.")
	group = frappe.db.get_value(
		"Student Group", student_group, ["name", "student_group_name", "program", "batch"], as_dict=True
	)
	days = week_days(week_start)

	only_instructor = only_courses = None
	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import teaching_pairs

		only_instructor = resolve_scope(persona).get("instructor")
		if not only_instructor or student_group not in set(instructor_groups(only_instructor, period=False)):
			frappe.throw(frappe._("This section is not yours."), frappe.PermissionError)
		only_courses = set(teaching_pairs(only_instructor).get(student_group, []))

	subjects = _subjects(student_group, group.program, days, only_instructor, only_courses)
	# The browser sends the Umm al-Qura dates. Without them the line is left to
	# fill by hand, as on the paper — an arithmetical guess can be two days out.
	hijri = (_hijri_arg(hijri_from), _hijri_arg(hijri_to))
	try:
		html = render(print_designs.effective(PRINT_KEY), group, days, subjects, hijri)
	except forms_print.DesignError as exc:
		return fail(
			message_en="The print design could not be rendered.",
			message_ar=f"{exc} — راجع «الخطة الأسبوعية» في الإعدادات › تصاميم الطباعة.",
		)
	return {
		"html": html,
		"title": f"الخطة الأسبوعية — {group.student_group_name or group.name} — {_gregorian(days[0])}",
		"weekStart": str(days[0]),
		"prepared": sum(1 for s in subjects for cell in s["cells"] if cell),
	}


def print_sample(design) -> str:
	"""The latest week anyone prepared, or a made-up one when nobody has."""
	last = frappe.get_all(
		"MS Lesson Plan", fields=["student_group", "schedule_date"], order_by="modified desc", limit=1
	)
	if last and last[0].student_group and frappe.db.exists("Student Group", last[0].student_group):
		g = frappe.db.get_value(
			"Student Group", last[0].student_group, ["name", "student_group_name", "program", "batch"], as_dict=True
		)
		days = week_days(str(last[0].schedule_date or nowdate()))
		subjects = _subjects(g.name, g.program, days, None)
		return render(design, g, days, subjects, ("", ""))

	days = week_days(nowdate())
	sample = [
		("القرآن الكريم", {0: ("سورة الملك: الآيات 1–10", "تلاوة وتجويد", "حفظ الآيات 1–5")}),
		("التربية الإسلامية", {1: ("آداب الحوار", "", "")}),
		("اللغة العربية", {0: ("درس: الجملة الاسمية", "", "تمارين ص 23"), 2: ("قراءة: وطني", "", "")}),
		("الرياضيات", {0: ("المعادلات الخطية", "حل معادلة بمتغير واحد", "ص 41: 1–6"), 3: ("اختبار قصير", "", "")}),
		("اللغة الإنجليزية", {1: ("Unit 3: Lesson 2", "", "Workbook p.18")}),
		("الفيزياء", {2: ("الحركة في خط مستقيم", "", "")}),
		("الكيمياء", {4: ("الجدول الدوري", "", "")}),
	]
	subjects = [
		{
			"id": f"s{i}",
			"name": name,
			"cells": [
				[{"title": by[d][0], "objectives": "", "content": by[d][1], "homework": by[d][2], "teacher": ""}]
				if d in by
				else []
				for d in range(5)
			],
		}
		for i, (name, by) in enumerate(sample)
	]
	group = {"program": "الصف العاشر", "batch": "أ"}
	return render(design, group, days, subjects, ("", ""))
