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

# Longest first, so "الحادي عشر" is not read as "الأول".
ORDINALS = [
	("الحادي عشر", 11), ("الثاني عشر", 12), ("الاول", 1), ("الثاني", 2), ("الثالث", 3),
	("الرابع", 4), ("الخامس", 5), ("السادس", 6), ("السابع", 7), ("الثامن", 8),
	("التاسع", 9), ("العاشر", 10),
]

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
	number = next((n for word, n in ORDINALS if word in _norm(label)), None)
	if not number:
		return ""
	original = re.sub(r"\s+", " ", str(label)).strip()
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
def import_template(persona: str = None):
	"""The spreadsheet to fill in, already holding the timetable as it stands.

	Pre-filled rather than blank: a school that has entered half its week on
	screen downloads that half, adds the rest, and uploads the lot — the file
	and the system never disagree about what is already there.
	"""
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

	slots, new_quota, problems = _read(uploaded.stream.read())
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
