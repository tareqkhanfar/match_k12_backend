# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The whole school's week from one spreadsheet, laid out like the paper one.

Schools moving off paper keep their timetable as a grid: one row per teacher,
one column per day and period, and a short section code such as "5ب" in each
cell. Retyping that into a screen teacher by teacher is days of work, and
ERPNext's generic Data Import writes slots straight to the table with none of
the checks the timetable screens apply.

This reads that grid as it is. Nothing is written until the whole file passes
the same checks the teacher screen's save runs, across every teacher at once:
a section promised to two teachers in one period is caught even when both
rows are in the same file.
"""

import base64
import io
import re

import frappe
from frappe.utils import cint

from match_schools.api import scheduling as sched
from match_schools.api import timetable_grid as tg
from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	apply_period,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	sync_group_instructors,
)

SHEET = "الجدول"
SECTIONS_SHEET = "الشعب"
COURSES_SHEET = "المواد"
HELP_SHEET = "تعليمات"

DAY_AR = {
	"Sunday": "الأحد", "Monday": "الاثنين", "Tuesday": "الثلاثاء",
	"Wednesday": "الأربعاء", "Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت",
}

# Longest first, so "الحادي عشر" is not read as "الأول". Kindergarten has no
# grade number; "ت" is the letter schools write for it.
ORDINALS = [
	("الحادي عشر", "11"), ("الثاني عشر", "12"), ("التمهيدي", "ت"), ("الاول", "1"),
	("الثاني", "2"), ("الثالث", "3"), ("الرابع", "4"), ("الخامس", "5"), ("السادس", "6"),
	("السابع", "7"), ("الثامن", "8"), ("التاسع", "9"), ("العاشر", "10"),
]


def _bare(label) -> str:
	"""A section's name without the academic year it carries: "(2026-2027)"."""
	return re.sub(r"\s*\([^)]*\)\s*", " ", str(label or "")).strip()

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _norm(value) -> str:
	"""Text as a person means it: digits, alef forms and spacing unified.

	"٥ ب", "5ب" and "5 ب" are the same section to whoever typed them, and a
	name typed with إ rather than ا is the same teacher.
	"""
	if value is None:
		return ""
	text = str(value).translate(ARABIC_DIGITS).replace("ـ", "")
	text = re.sub("[إأآ]", "ا", text)
	return re.sub(r"\s+", " ", text).strip()


def _code_key(value) -> str:
	return _norm(value).replace(" ", "").replace("-", "")


def _derive_code(label: str) -> str:
	"""A short code from a section's name: "الصف الخامس - أ" → "5أ".

	The number is read from normalised text; the letter is kept as the school
	wrote it, because the code is what staff will see and type.
	"""
	number = next((n for word, n in ORDINALS if word in _norm(_bare(label))), None)
	if not number:
		return ""
	original = re.sub(r"\s+", " ", _bare(label)).strip()
	tail = original.split("-")[-1].strip() if "-" in original else original.split(" ")[-1]
	if not tail or len(tail) > 2:
		return ""
	return f"{number}{tail}"


def _teaching_periods() -> tuple[list[dict], list[str]]:
	"""Teaching periods in order, numbered 1..n as a school counts them.

	The stored order counts breaks too (period 3 may be the break), so the
	sheet's "الأحد 3" is the third lesson of the day, not stored order 3.
	"""
	periods, working_days = tg.school_grid()
	return [p for p in periods if not p.get("isBreak")], working_days


# --- Lookups ------------------------------------------------------------------


def _groups() -> list[dict]:
	return frappe.get_all(
		"Student Group",
		filters=apply_period({"disabled": 0}, "Student Group"),
		fields=["name", "student_group_name", "program"],
		order_by="program, student_group_name",
		limit_page_length=0,
	)


def _auto_codes(groups: list[dict]) -> dict[str, str]:
	"""group → code, dropping any code two sections would share."""
	codes = {g.name: _derive_code(g.student_group_name or g.name) for g in groups}
	counts: dict[str, int] = {}
	for c in codes.values():
		if c:
			counts[c] = counts.get(c, 0) + 1
	return {g: c for g, c in codes.items() if c and counts[c] == 1}


def _instructors() -> list[dict]:
	return frappe.get_all(
		"Instructor",
		filters={"status": "Active"},
		fields=["name", "instructor_name", "ms_weekly_quota"],
		order_by="instructor_name",
		limit_page_length=0,
	)


def _courses() -> list[dict]:
	return frappe.get_all("Course", fields=["name", "course_name"], order_by="name", limit_page_length=0)


def _q(value) -> str:
	"""A typed value quoted and bidi-isolated: «99ز» would otherwise render as
	«ز99» or worse inside an Arabic sentence."""
	return f"«\u2068{value}\u2069»"


def _cell_ref(row: int, col: int) -> str:
	from openpyxl.utils import get_column_letter

	return f"{get_column_letter(col)}{row}"


# --- Template -----------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def import_template(format: str = "flat", persona: str = None):
	"""The spreadsheet to fill in, already holding the timetable as it stands.

	Pre-filled rather than blank: a school that has entered half its week on
	screen downloads that half, adds the rest, and uploads the lot — the file
	and the system never disagree about what is already there.
	"""
	if format != "grid":
		return _flat_template()

	from openpyxl import Workbook
	from openpyxl.styles import Alignment, Font, PatternFill

	periods, working_days = _teaching_periods()
	days = [d for d, _ in sched.WEEKDAYS if d in working_days]

	groups = _groups()
	codes = _auto_codes(groups)
	instructors = _instructors()

	slots = frappe.get_all(
		"MS Timetable Slot",
		filters={"active": 1},
		fields=["instructor", "day", "period_order", "student_group", "course"],
		limit_page_length=0,
	)
	by_teacher: dict[str, list] = {}
	for s in slots:
		if s.instructor:
			by_teacher.setdefault(s.instructor, []).append(s)

	wb = Workbook()
	ws = wb.active
	ws.title = SHEET
	ws.sheet_view.rightToLeft = True

	head = ["المعلم", "المادة", "النصاب"] + [
		f"{DAY_AR[d]} {i + 1}" for d in days for i, _ in enumerate(periods)
	]
	ws.append(head)
	bold = Font(bold=True)
	fill = PatternFill("solid", fgColor="E8EEF9")
	for col in range(1, len(head) + 1):
		cell = ws.cell(row=1, column=col)
		cell.font = bold
		cell.fill = fill
		cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

	col_of = {(d, p["order"]): 4 + i for i, (d, p) in enumerate((d, p) for d in days for p in periods)}

	for ins in instructors:
		mine = by_teacher.get(ins.name, [])
		# The subject a teacher teaches most becomes the row's default, so the
		# cells need only the section — as on paper.
		tally: dict[str, int] = {}
		for s in mine:
			tally[s.course] = tally.get(s.course, 0) + 1
		default = max(tally, key=tally.get) if tally else ""

		row = [ins.name, default, cint(ins.ms_weekly_quota) or None] + [None] * (len(head) - 3)
		for s in mine:
			col = col_of.get((s.day, cint(s.period_order)))
			if not col:
				continue
			ref = codes.get(s.student_group) or s.student_group
			row[col - 1] = ref if s.course == default else f"{ref}:{s.course}"
		ws.append(row)

	ws.freeze_panes = "D2"
	ws.column_dimensions["A"].width = 26
	ws.column_dimensions["B"].width = 16
	ws.column_dimensions["C"].width = 8
	from openpyxl.utils import get_column_letter

	for col in range(4, len(head) + 1):
		ws.column_dimensions[get_column_letter(col)].width = 9

	sec = wb.create_sheet(SECTIONS_SHEET)
	sec.sheet_view.rightToLeft = True
	sec.append(["الرمز", "اسم الشعبة في النظام", "مواد الشعبة"])
	for col in range(1, 4):
		sec.cell(row=1, column=col).font = bold
	for g in groups:
		sec.append([codes.get(g.name, ""), g.name, "، ".join(tg._courses_for_group(g.name))])
	sec.column_dimensions["A"].width = 10
	sec.column_dimensions["B"].width = 30
	sec.column_dimensions["C"].width = 80

	crs = wb.create_sheet(COURSES_SHEET)
	crs.sheet_view.rightToLeft = True
	crs.append(["اسم المادة في النظام"])
	crs.cell(row=1, column=1).font = bold
	for c in _courses():
		crs.append([c.name])
	crs.column_dimensions["A"].width = 30

	help_ = wb.create_sheet(HELP_SHEET)
	help_.sheet_view.rightToLeft = True
	for line in [
		"طريقة التعبئة",
		"• كل صف = معلم واحد. عمود «المادة» هو المادة التي يدرّسها غالباً.",
		"• في كل خلية حصة اكتب رمز الشعبة فقط، مثل: 5ب — وتُسجَّل الحصة بمادة المعلم.",
		"• إن كانت الحصة بمادة أخرى اكتب: 5ب:الرياضيات",
		"• رموز الشعب في ورقة «الشعب»، ويمكنك تعديل الرمز ليطابق ورقتكم. يُقبل أيضاً اسم الشعبة كاملاً.",
		"• النصاب (اختياري): أقصى عدد حصص أسبوعية للمعلم. يُرفض الملف إن تجاوزه.",
		"• الصف الفارغ تماماً لا يُمسّ؛ أما الصف المعبّأ فيستبدل جدول ذلك المعلم بالكامل.",
		"• لا يُحفظ شيء حتى يخلو الملف كله من الأخطاء، وتظهر لك الأخطاء بموقع الخلية.",
	]:
		help_.append([line])
	help_.column_dimensions["A"].width = 100
	help_.cell(row=1, column=1).font = Font(bold=True, size=13)

	buffer = io.BytesIO()
	wb.save(buffer)
	return {
		"filename": "جدول-المعلمين.xlsx",
		"content": base64.b64encode(buffer.getvalue()).decode(),
	}


# --- Import -------------------------------------------------------------------


def _read(content: bytes):
	"""Parse the file into slots per teacher, collecting every problem found."""
	from openpyxl import load_workbook

	problems: list[dict] = []

	def problem(row, col, message, teacher=None):
		problems.append({
			"row": row,
			"cell": _cell_ref(row, col) if row and col else None,
			"teacher": teacher,
			"message": message,
		})

	try:
		wb = load_workbook(io.BytesIO(content), data_only=True)
	except Exception:
		problem(None, None, "الملف ليس ملف إكسل صالحاً (xlsx).")
		return {}, {}, problems

	ws = wb[SHEET] if SHEET in wb.sheetnames else wb.worksheets[0]

	periods, working_days = _teaching_periods()
	order_of = {i + 1: cint(p["order"]) for i, p in enumerate(periods)}
	day_of = {_norm(ar): en for en, ar in DAY_AR.items()}
	day_of.update({_norm(ar).replace("ال", "", 1): en for en, ar in DAY_AR.items()})

	# --- Header: which column is which day and period.
	columns: dict[int, tuple[str, int]] = {}
	teacher_col = subject_col = quota_col = None
	for col in range(1, ws.max_column + 1):
		raw = _norm(ws.cell(row=1, column=col).value)
		if not raw:
			continue
		if raw in ("المعلم", "المعلمة", "اسم المعلم"):
			teacher_col = col
			continue
		if raw in ("المادة", "المادة الافتراضية"):
			subject_col = col
			continue
		if raw == "النصاب":
			quota_col = col
			continue
		match = re.match(r"^(.*?)[\s\-_/]*(\d+)$", raw)
		day = day_of.get(_norm(match.group(1))) if match else None
		if not match or not day:
			problem(1, col, f"عنوان عمود غير مفهوم: {_q(raw)} — المتوقع مثل «الأحد 1».")
			continue
		number = cint(match.group(2))
		if day not in working_days:
			problem(1, col, f"{_q(raw)}: {DAY_AR[day]} ليس يوم دوام في المدرسة.")
			continue
		if number not in order_of:
			problem(
				1, col,
				f"{_q(raw)}: اليوم الدراسي فيه {len(order_of)} حصص فقط.",
			)
			continue
		columns[col] = (day, order_of[number])

	if not teacher_col:
		problem(1, 1, "لا يوجد عمود «المعلم» في الصف الأول.")
		return {}, {}, problems

	# --- Lookups for sections, teachers and courses.
	groups = _groups()
	group_by: dict[str, str] = {}
	for g in groups:
		group_by[_code_key(g.name)] = g.name
		if g.student_group_name:
			group_by[_code_key(g.student_group_name)] = g.name
		# The name as a person writes it, without the year in brackets.
		group_by.setdefault(_code_key(_bare(g.student_group_name or g.name)), g.name)
	codes = _auto_codes(groups)
	if SECTIONS_SHEET in wb.sheetnames:
		# The school's own codes win: they are what is written on its paper.
		sec = wb[SECTIONS_SHEET]
		for r in range(2, sec.max_row + 1):
			code, name = sec.cell(row=r, column=1).value, sec.cell(row=r, column=2).value
			if code and name:
				target = group_by.get(_code_key(name))
				if target:
					codes[target] = _norm(code)
	for group, code in codes.items():
		group_by.setdefault(_code_key(code), group)

	teacher_by: dict[str, str] = {}
	quotas: dict[str, int] = {}
	for i in _instructors():
		teacher_by[_norm(i.name)] = i.name
		if i.instructor_name:
			teacher_by.setdefault(_norm(i.instructor_name), i.name)
		quotas[i.name] = cint(i.ms_weekly_quota)

	course_by: dict[str, str] = {}
	for c in _courses():
		course_by[_norm(c.name)] = c.name
		if c.course_name:
			course_by.setdefault(_norm(c.course_name), c.name)

	group_courses: dict[str, set] = {}

	def courses_of(group: str) -> set:
		if group not in group_courses:
			group_courses[group] = set(tg._courses_for_group(group))
		return group_courses[group]

	# --- Rows.
	slots: dict[str, list[dict]] = {}
	new_quota: dict[str, int] = {}
	rows_of: dict[str, int] = {}
	for r in range(2, ws.max_row + 1):
		raw_teacher = _norm(ws.cell(row=r, column=teacher_col).value)
		filled = [c for c in columns if _norm(ws.cell(row=r, column=c).value)]
		if not raw_teacher:
			if filled:
				problem(r, teacher_col, "صف فيه حصص دون اسم معلم.")
			continue
		teacher = teacher_by.get(raw_teacher)
		if not teacher:
			problem(r, teacher_col, f"المعلم {_q(raw_teacher)} غير موجود في النظام.")
			continue
		if not filled:
			# A blank row leaves that teacher's week exactly as it is.
			continue
		if teacher in rows_of:
			problem(r, teacher_col, f"المعلم مكرر — ورد أيضاً في الصف {rows_of[teacher]}.", teacher)
			continue
		rows_of[teacher] = r

		default_course = None
		if subject_col:
			raw_subject = _norm(ws.cell(row=r, column=subject_col).value)
			if raw_subject:
				default_course = course_by.get(raw_subject)
				if not default_course:
					problem(r, subject_col, f"المادة {_q(raw_subject)} غير موجودة في النظام.", teacher)

		if quota_col:
			raw_quota = ws.cell(row=r, column=quota_col).value
			if raw_quota not in (None, ""):
				try:
					new_quota[teacher] = int(float(_norm(raw_quota)))
				except ValueError:
					problem(r, quota_col, f"النصاب {_q(raw_quota)} ليس رقماً.", teacher)

		mine = []
		for col in filled:
			day, order = columns[col]
			text = _norm(ws.cell(row=r, column=col).value)
			parts = re.split(r"\s*[:：]\s*", text, maxsplit=1)
			section_ref = parts[0]
			group = group_by.get(_code_key(section_ref))
			if not group:
				problem(r, col, f"الشعبة {_q(section_ref)} غير معروفة — راجع ورقة «الشعب».", teacher)
				continue
			if len(parts) > 1 and parts[1]:
				course = course_by.get(_norm(parts[1]))
				if not course:
					problem(r, col, f"المادة {_q(parts[1])} غير موجودة في النظام.", teacher)
					continue
			else:
				course = default_course
				if not course:
					problem(
						r, col,
						"لا مادة لهذه الحصة — عبّئ عمود «المادة» أو اكتب «الشعبة:المادة».",
						teacher,
					)
					continue
			if course not in courses_of(group):
				problem(r, col, f"{_q(course)} ليست من مواد الشعبة {_q(group)}.", teacher)
				continue
			mine.append({
				"day": day,
				"period": order,
				"studentGroup": group,
				"student_group": group,
				"course": course,
				"room": None,
				"row": r,
				"col": col,
			})
		slots[teacher] = mine

	# Quota: from the file when given, otherwise what the teacher already has.
	for teacher, mine in slots.items():
		cap = new_quota.get(teacher, quotas.get(teacher, 0))
		if cap and len(mine) > cap:
			problem(
				rows_of[teacher], quota_col or teacher_col,
				f"{len(mine)} حصة تتجاوز النصاب الأسبوعي ({cap}).", teacher,
			)

	return slots, new_quota, problems


def _cross_check(slots: dict[str, list[dict]]) -> list[dict]:
	"""Clashes across the whole file and against everything outside it."""
	problems: list[dict] = []
	importing = set(slots)

	def problem(s, message, teacher):
		problems.append({
			"row": s["row"], "cell": _cell_ref(s["row"], s["col"]),
			"teacher": teacher, "message": message,
		})

	names = dict(
		frappe.get_all("Instructor", fields=["name", "instructor_name"], as_list=True)
	)

	# A section in one period can have one lesson. Teachers outside the file
	# keep their slots, so those count as already taken.
	holder: dict[tuple, tuple] = {}
	for r in frappe.get_all(
		"MS Timetable Slot",
		filters={"active": 1},
		fields=["day", "period_order", "student_group", "instructor"],
		limit_page_length=0,
	):
		if r.instructor in importing:
			continue
		holder[(r.day, cint(r.period_order), r.student_group)] = (r.instructor, None)

	for teacher, mine in slots.items():
		for s in mine:
			key = (s["day"], s["period"], s["studentGroup"])
			if key in holder:
				other, other_row = holder[key]
				where = f" (الصف {other_row})" if other_row else ""
				problem(
					s,
					f"الشعبة {s['studentGroup']} محجوزة في هذه الحصة "
					f"لدى {names.get(other) or other or 'حصة أخرى'}{where}.",
					teacher,
				)
			else:
				holder[key] = (teacher, s["row"])

	# Lessons already generated for the term, and each teacher's unavailable
	# times: the same check the teacher screen's save runs. The lessons of the
	# teachers being imported are about to be regenerated, so they are set aside.
	exclude = set(
		frappe.get_all(
			"Course Schedule",
			filters={"instructor": ["in", list(importing) or [""]], "docstatus": ["<", 2]},
			pluck="name",
		)
	)
	for teacher, mine in slots.items():
		lessons = [
			{
				"day": s["day"],
				"from_time": tg._period_time(s, "from"),
				"to_time": tg._period_time(s, "to"),
				"instructor": teacher,
				"room": None,
				"student_group": s["studentGroup"],
			}
			for s in mine
		]
		conflicts = sched.find_conflicts(lessons, exclude=exclude, academic_term=None)
		for index, items in conflicts.items():
			if index < len(mine) and items:
				problem(mine[index], items[0]["detail"], teacher)

	return problems


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def import_timetable(commit: int = 0, persona: str = None):
	"""Check an uploaded timetable, and write it when asked and when clean.

	Always checks first; `commit=1` writes only if the check finds nothing.
	The write is one transaction — a failure part-way leaves the week as it
	was, never half-imported.
	"""
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail("No file uploaded.", "لم يتم رفع أي ملف.")
	content = uploaded.stream.read()
	filename = getattr(uploaded, "filename", "") or ""

	try:
		table = _table(filename, content)
	except Exception:
		return fail("The file could not be read.", "تعذّرت قراءة الملف — ارفع ملف CSV أو Excel (xlsx).")
	cols = _flat_columns(table[0]) if table else None
	if cols:
		return _import_flat(table, cols, cint(commit))
	if filename.lower().endswith(".csv"):
		return fail(
			"Unrecognised columns.",
			"أعمدة الملف غير معروفة — المتوقع: Student Group, Day, Period, From, To, Course, Instructor.",
		)

	slots, new_quota, problems = _read(content)
	# Every mistake in one pass: a school fixing its file should not find the
	# clashes only after correcting the typos.
	problems += _cross_check(slots)

	names = dict(frappe.get_all("Instructor", fields=["name", "instructor_name"], as_list=True))
	summary = {
		"teachers": [
			{
				"name": t,
				"label": names.get(t) or t,
				"lessons": len(mine),
				"groups": len({s["studentGroup"] for s in mine}),
				"quota": new_quota.get(t),
			}
			for t, mine in sorted(slots.items(), key=lambda kv: names.get(kv[0]) or kv[0])
		],
		"lessons": sum(len(m) for m in slots.values()),
		"groups": len({s["studentGroup"] for m in slots.values() for s in m}),
		"problems": sorted(problems, key=lambda p: (p["row"] or 0, p["cell"] or "")),
	}

	if not cint(commit) or problems:
		summary["committed"] = False
		return summary

	academic_year = get_default_academic_year()
	academic_term = get_default_academic_term()
	touched: set[str] = set()
	try:
		for teacher, mine in slots.items():
			for existing in frappe.get_all(
				"MS Timetable Slot", filters={"instructor": teacher}, fields=["name", "student_group"]
			):
				touched.add(existing.student_group)
				frappe.delete_doc(
					"MS Timetable Slot", existing.name, ignore_permissions=True, force=True
				)
			for s in mine:
				doc = frappe.new_doc("MS Timetable Slot")
				doc.student_group = s["studentGroup"]
				doc.day = s["day"]
				doc.period_order = s["period"]
				doc.from_time = tg._period_time(s, "from")
				doc.to_time = tg._period_time(s, "to")
				doc.course = s["course"]
				doc.instructor = teacher
				doc.academic_year = academic_year
				doc.academic_term = academic_term
				doc.active = 1
				doc.insert(ignore_permissions=True)
				touched.add(s["studentGroup"])
			if teacher in new_quota:
				frappe.db.set_value("Instructor", teacher, "ms_weekly_quota", new_quota[teacher])
			tg._sync_plans(teacher, mine)

		for group in touched:
			if group:
				sync_group_instructors(group)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Timetable import failed")
		return fail(
			"The import failed and nothing was saved.",
			"تعذّر الاستيراد ولم يُحفظ أي شيء — الجدول كما كان.",
		)

	summary["committed"] = True
	return summary


# --- The flat format: one row per lesson, as ERPNext's Data Import writes it --

# Column titles as Data Import's own template spells them, the fieldnames, and
# the Arabic a school may type instead — all mean the same column.
FLAT_COLUMNS = {
	"student group": "student_group", "student_group": "student_group", "الشعبة": "student_group",
	"day": "day", "اليوم": "day",
	"period": "period_order", "period_order": "period_order", "الحصة": "period_order",
	"from": "from_time", "from_time": "from_time", "من": "from_time",
	"to": "to_time", "to_time": "to_time", "الى": "to_time", "إلى": "to_time",
	"course": "course", "المادة": "course",
	"instructor": "instructor", "المعلم": "instructor",
	"room": "room", "القاعة": "room",
	"academic year": "academic_year", "academic_year": "academic_year", "العام الدراسي": "academic_year",
	"academic term": "academic_term", "academic_term": "academic_term", "الفصل الدراسي": "academic_term",
	"active": "active", "فعال": "active",
}
FLAT_HEADER = ["Student Group", "Day", "Period", "From", "To", "Course", "Instructor", "Academic Year", "Active"]
FLAT_LABEL = {
	"student_group": "Student Group", "day": "Day", "period_order": "Period", "from_time": "From",
	"to_time": "To", "course": "Course", "instructor": "Instructor", "room": "Room",
	"academic_year": "Academic Year", "academic_term": "Academic Term", "active": "Active",
}
DAY_BY_TEXT = {**{d.lower(): d for d in DAY_AR}, **{_norm(ar): en for en, ar in DAY_AR.items()}}


def _table(filename: str, content: bytes) -> list[list]:
	"""Rows of the uploaded sheet, CSV or Excel, header first."""
	if (filename or "").lower().endswith(".csv"):
		import csv

		text = content.decode("utf-8-sig", errors="replace")
		return [row for row in csv.reader(io.StringIO(text))]
	from openpyxl import load_workbook

	wb = load_workbook(io.BytesIO(content), data_only=True)
	ws = wb[SHEET] if SHEET in wb.sheetnames else wb.worksheets[0]
	return [list(r) for r in ws.iter_rows(values_only=True)]


def _flat_columns(header: list) -> dict[str, int] | None:
	"""Column positions when the header is the flat format, else None."""
	cols = {}
	for i, title in enumerate(header):
		key = FLAT_COLUMNS.get(_norm(title).lower()) if title is not None else None
		if key and key not in cols:
			cols[key] = i
	needed = {"student_group", "day", "period_order", "course"}
	return cols if needed <= set(cols) else None


def _time(value) -> str | None:
	"""A time from whatever a spreadsheet hands over: text, a time, a fraction."""
	import datetime

	if value in (None, ""):
		return None
	if isinstance(value, datetime.datetime):
		value = value.time()
	if isinstance(value, datetime.time):
		return value.strftime("%H:%M:%S")
	if isinstance(value, (int, float)) and 0 <= value < 1:
		seconds = round(value * 86400)
		return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
	match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?", _norm(value))
	if not match:
		return None
	h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
	if h > 23 or m > 59 or s > 59:
		return None
	return f"{h:02d}:{m:02d}:{s:02d}"


def _read_flat(table: list[list], cols: dict[str, int]):
	"""One lesson per row, each resolved and checked on its own."""
	problems: list[dict] = []

	def problem(row, field, message):
		col = cols.get(field)
		problems.append({
			"row": row,
			"cell": _cell_ref(row, col + 1) if col is not None else f"{row}",
			"teacher": None,
			"message": message,
		})

	def get(row, field):
		i = cols.get(field)
		return row[i] if i is not None and i < len(row) else None

	groups = _groups()
	# Every current section, not only this term's: a file naming a section by
	# its full id should resolve whichever year it belongs to.
	for g in frappe.get_all(
		"Student Group", filters={"disabled": 0}, fields=["name", "student_group_name", "program"],
		limit_page_length=0,
	):
		if g.name not in {x.name for x in groups}:
			groups.append(g)
	group_by: dict[str, str] = {}
	for g in groups:
		group_by[_code_key(g.name)] = g.name
		if g.student_group_name:
			group_by.setdefault(_code_key(g.student_group_name), g.name)
		group_by.setdefault(_code_key(_bare(g.student_group_name or g.name)), g.name)
	for group, code in _auto_codes(groups).items():
		group_by.setdefault(_code_key(code), group)

	teacher_by: dict[str, str] = {}
	for i in frappe.get_all("Instructor", fields=["name", "instructor_name"], limit_page_length=0):
		teacher_by[_norm(i.name)] = i.name
		if i.instructor_name:
			teacher_by.setdefault(_norm(i.instructor_name), i.name)
	course_by: dict[str, str] = {}
	for c in _courses():
		course_by[_norm(c.name)] = c.name
		if c.course_name:
			course_by.setdefault(_norm(c.course_name), c.name)
	rooms = set(frappe.get_all("Room", pluck="name"))
	years = set(frappe.get_all("Academic Year", pluck="name"))
	terms = set(frappe.get_all("Academic Term", pluck="name"))
	group_courses: dict[str, set] = {}

	entries: list[dict] = []
	for index, row in enumerate(table[1:], start=2):
		if not any(v not in (None, "") and str(v).strip() for v in row):
			continue
		bad = len(problems)

		raw_group = _norm(get(row, "student_group"))
		group = group_by.get(_code_key(raw_group)) if raw_group else None
		if not group:
			problem(index, "student_group", f"الشعبة {_q(raw_group)} غير موجودة في النظام." if raw_group else "الشعبة فارغة.")

		raw_day = _norm(get(row, "day"))
		day = DAY_BY_TEXT.get(raw_day.lower()) or DAY_BY_TEXT.get(raw_day)
		if not day:
			problem(index, "day", f"اليوم {_q(raw_day)} غير مفهوم — اكتب Sunday … Thursday أو الأحد … الخميس.")

		try:
			period = int(float(_norm(get(row, "period_order"))))
			if period < 1:
				raise ValueError
		except (TypeError, ValueError):
			period = None
			problem(index, "period_order", f"رقم الحصة {_q(get(row, 'period_order'))} غير صالح.")

		raw_course = _norm(get(row, "course"))
		course = course_by.get(raw_course) if raw_course else None
		if not course:
			problem(index, "course", f"المادة {_q(raw_course)} غير موجودة في النظام." if raw_course else "المادة فارغة.")
		elif group:
			if group not in group_courses:
				group_courses[group] = set(tg._courses_for_group(group))
			if course not in group_courses[group]:
				problem(index, "course", f"{_q(course)} ليست من مواد الشعبة {_q(group)}.")

		raw_teacher = _norm(get(row, "instructor"))
		teacher = teacher_by.get(raw_teacher) if raw_teacher else None
		if not raw_teacher:
			problem(index, "instructor", "المعلم فارغ — كل حصة تحتاج معلماً.")
		elif not teacher:
			problem(index, "instructor", f"المعلم {_q(raw_teacher)} غير موجود في النظام.")

		room = _norm(get(row, "room")) or None
		if room and room not in rooms:
			problem(index, "room", f"القاعة {_q(room)} غير موجودة في النظام.")

		year = _norm(get(row, "academic_year")) or None
		if year and year not in years:
			problem(index, "academic_year", f"العام الدراسي {_q(year)} غير موجود.")
		term = _norm(get(row, "academic_term")) or None
		if term and term not in terms:
			problem(index, "academic_term", f"الفصل الدراسي {_q(term)} غير موجود.")

		raw_active = _norm(get(row, "active"))
		active = 0 if raw_active in ("0", "false", "False", "لا") else 1

		start, end = _time(get(row, "from_time")), _time(get(row, "to_time"))
		if get(row, "from_time") not in (None, "") and not start:
			problem(index, "from_time", f"الوقت {_q(get(row, 'from_time'))} غير مفهوم — مثل 08:00:00.")
		if get(row, "to_time") not in (None, "") and not end:
			problem(index, "to_time", f"الوقت {_q(get(row, 'to_time'))} غير مفهوم — مثل 08:45:00.")
		if group and period and not (start and end) and len(problems) == bad:
			# Left blank: the period's own clock supplies the time.
			slot = {"period": period, "student_group": group}
			start = start or tg._period_time(slot, "from") or None
			end = end or tg._period_time(slot, "to") or None
			if not (start and end):
				problem(index, "from_time", f"لا وقت للحصة {period} — اكتب وقتي البداية والنهاية.")
		if start and end and start >= end:
			problem(index, "to_time", "وقت النهاية يجب أن يكون بعد وقت البداية.")

		if len(problems) == bad:
			entries.append({
				"row": index, "student_group": group, "day": day, "period": period,
				"from_time": start, "to_time": end, "course": course, "instructor": teacher,
				"room": room, "academic_year": year, "academic_term": term, "active": active,
			})

	return entries, problems


def _check_flat(entries: list[dict], cols: dict[str, int]) -> list[dict]:
	"""Clashes by time, within the file and against everything outside it.

	The file replaces the week of the sections it names. Slots of every other
	section stay, so they are placed first in the list the conflict engine
	walks: each file row is then checked against them and against the rows
	above it, and a clash is reported on the file's own row.
	"""
	problems: list[dict] = []
	groups_in_file = {e["student_group"] for e in entries}
	live = [e for e in entries if e["active"]]

	kept = [
		{
			"day": r.day, "from_time": sched.hhmmss(r.from_time), "to_time": sched.hhmmss(r.to_time),
			"instructor": r.instructor, "room": r.room, "student_group": r.student_group,
		}
		for r in frappe.get_all(
			"MS Timetable Slot",
			filters={"active": 1, "student_group": ["not in", list(groups_in_file) or [""]]},
			fields=["day", "from_time", "to_time", "instructor", "room", "student_group"],
			limit_page_length=0,
		)
	]
	# Lessons already generated for the sections in the file are about to be
	# regenerated from it; every other section's lessons are real bookings.
	exclude = set(
		frappe.get_all(
			"Course Schedule",
			filters={"student_group": ["in", list(groups_in_file) or [""]], "docstatus": ["<", 2]},
			pluck="name",
		)
	)
	lessons = kept + [
		{
			"day": e["day"], "from_time": e["from_time"], "to_time": e["to_time"],
			"instructor": e["instructor"], "room": e["room"], "student_group": e["student_group"],
		}
		for e in live
	]
	conflicts = sched.find_conflicts(lessons, exclude=exclude, academic_term=None)
	offset = len(kept)
	col = cols.get("student_group", 0)
	for index, items in sorted(conflicts.items()):
		if index < offset:
			continue
		e = live[index - offset]
		for item in items:
			message = item["detail"]
			if item.get("kind") in ("student_group", "instructor", "room") and not item.get("with"):
				# A clash inside the file: name the row it clashes with, so the
				# fix is one look away rather than a search through the sheet.
				partner = next(
					(
						o["row"]
						for o in live[: index - offset]
						if o["day"] == e["day"]
						and o.get(item["kind"]) == e.get(item["kind"])
						and sched.overlaps(e["from_time"], e["to_time"], o["from_time"], o["to_time"])
					),
					None,
				)
				if partner:
					message = f"يتعارض مع الصف {partner}: {message}"
			problems.append({
				"row": e["row"], "cell": _cell_ref(e["row"], col + 1), "teacher": e["instructor"],
				"message": message,
			})

	# النصاب: the file's lessons plus what the teacher keeps in other sections.
	load: dict[str, int] = {}
	for lesson in [k for k in kept] + [e for e in live]:
		if lesson.get("instructor"):
			load[lesson["instructor"]] = load.get(lesson["instructor"], 0) + 1
	quotas = dict(
		frappe.get_all(
			"Instructor",
			filters={"name": ["in", list(load) or [""]]},
			fields=["name", "ms_weekly_quota"],
			as_list=True,
		)
	)
	for teacher, count in sorted(load.items()):
		cap = cint(quotas.get(teacher))
		if cap and count > cap:
			first = next(e for e in live if e["instructor"] == teacher)
			problems.append({
				"row": first["row"], "cell": None, "teacher": teacher,
				"message": f"{count} حصة تتجاوز النصاب الأسبوعي للمعلم ({cap}).",
			})
	return problems


def _commit_flat(entries: list[dict]) -> None:
	"""Replace the week of every section in the file, in one transaction."""
	groups_in_file = {e["student_group"] for e in entries}
	year = get_default_academic_year()
	term = get_default_academic_term()
	teachers: set[str] = set()
	for existing in frappe.get_all(
		"MS Timetable Slot",
		filters={"student_group": ["in", list(groups_in_file)]},
		fields=["name", "instructor"],
	):
		if existing.instructor:
			teachers.add(existing.instructor)
		frappe.delete_doc("MS Timetable Slot", existing.name, ignore_permissions=True, force=True)

	for e in entries:
		doc = frappe.new_doc("MS Timetable Slot")
		doc.student_group = e["student_group"]
		doc.day = e["day"]
		doc.period_order = e["period"]
		doc.from_time = e["from_time"]
		doc.to_time = e["to_time"]
		doc.course = e["course"]
		doc.instructor = e["instructor"]
		doc.room = e["room"]
		doc.academic_year = e["academic_year"] or year
		doc.academic_term = e["academic_term"] or term
		doc.active = e["active"]
		doc.insert(ignore_permissions=True)
		teachers.add(e["instructor"])

	for teacher in teachers:
		if teacher:
			tg._sync_plans(
				teacher,
				[
					{"studentGroup": e["student_group"], "course": e["course"]}
					for e in entries
					if e["instructor"] == teacher and e["active"]
				],
			)
	for group in groups_in_file:
		sync_group_instructors(group)


def _flat_template() -> dict:
	"""Data Import's own columns, pre-filled with the week as it stands."""
	import csv

	order = {d: i for i, (d, _) in enumerate(sched.WEEKDAYS)}
	rows = frappe.get_all(
		"MS Timetable Slot",
		fields=[
			"student_group", "day", "period_order", "from_time", "to_time",
			"course", "instructor", "academic_year", "active",
		],
		limit_page_length=0,
	)
	rows.sort(key=lambda r: (r.student_group or "", order.get(r.day, 9), cint(r.period_order)))
	buffer = io.StringIO()
	writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
	writer.writerow(FLAT_HEADER)
	for r in rows:
		writer.writerow([
			r.student_group, r.day, cint(r.period_order),
			sched.hhmmss(r.from_time), sched.hhmmss(r.to_time),
			r.course, r.instructor or "", r.academic_year or "", cint(r.active),
		])
	# Excel opens UTF-8 CSV as mojibake without the byte-order mark.
	return {
		"filename": "MS_Timetable_Slot.csv",
		"content": base64.b64encode(("﻿" + buffer.getvalue()).encode("utf-8")).decode(),
	}


def _import_flat(table: list[list], cols: dict[str, int], commit: int) -> dict:
	entries, problems = _read_flat(table, cols)
	# Clashes are checked even when some rows failed, so the school sees every
	# mistake in one pass rather than one layer at a time.
	problems += _check_flat(entries, cols)

	names = dict(frappe.get_all("Instructor", fields=["name", "instructor_name"], as_list=True))
	by_teacher: dict[str, list] = {}
	for e in entries:
		by_teacher.setdefault(e["instructor"], []).append(e)
	summary = {
		"format": "flat",
		"teachers": [
			{
				"name": t,
				"label": names.get(t) or t,
				"lessons": len(mine),
				"groups": len({e["student_group"] for e in mine}),
				"quota": None,
			}
			for t, mine in sorted(by_teacher.items(), key=lambda kv: names.get(kv[0]) or kv[0])
		],
		"lessons": len(entries),
		"groups": len({e["student_group"] for e in entries}),
		"problems": sorted(problems, key=lambda p: (p["row"] or 0, p["cell"] or "")),
		"committed": False,
	}
	if not commit or problems or not entries:
		return summary
	try:
		_commit_flat(entries)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Timetable import failed")
		return fail(
			"The import failed and nothing was saved.",
			"تعذّر الاستيراد ولم يُحفظ أي شيء — الجدول كما كان.",
		)
	summary["committed"] = True
	return summary
