# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""تقارير القيادة — what the vice principal and the administration read.

The vice principal leads the teachers. His questions are about the whole
staff rather than one class: who has not entered their marks yet, who has not
opened the system this month, which sections went a day without attendance,
whose lessons have no log, who is carrying the covers and the duties.

Every report here only reads. Each one is a handful of grouped queries over
the period the screen asks about, joined in Python — never a loop of queries
per teacher or per student, so the same screen stays quick on a school ten
times the size of the demo one.

Who teaches what is taken from the same three places the rest of the app
uses (Course Schedule, the weekly MS Timetable Slot, a section's own subject),
narrowed to the sections of the selected year and term.
"""

import datetime

import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	apply_period,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
)

WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_AR = {
	"Sunday": "الأحد", "Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء",
	"Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت",
}
# The school week when nothing else says otherwise; the timetable can add to it.
DEFAULT_SCHOOL_DAYS = {"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"}
REASON_AR = {
	"Sick Leave": "إجازة مرضية",
	"Personal Leave": "إجازة شخصية",
	"Official Duty": "مهمة رسمية",
	"Training": "تدريب",
	"Exam": "امتحان",
	"Other": "سبب آخر",
	"": "غير محدد",
}
# A report over more than this many days is refused rather than left to crawl:
# the school-day calendar is walked day by day.
MAX_SPAN_DAYS = 400
# How many missing days a section row lists by date before it only counts them.
MAX_LISTED_DAYS = 40


# --- Period and filters ---------------------------------------------------------


def _range(date_from=None, date_to=None) -> tuple[datetime.date, datetime.date]:
	"""The days a report covers.

	Defaults to the selected term up to today — the question is nearly always
	"so far this term" — and to the last thirty days when no term is defined
	or the term has not started. Never runs past today: a lesson that has not
	happened yet is not a missing log.
	"""
	now = getdate(today())
	start = getdate(date_from) if date_from else None
	end = getdate(date_to) if date_to else None

	if not start or not end:
		term = get_default_academic_term()
		bounds = (
			frappe.db.get_value("Academic Term", term, ["term_start_date", "term_end_date"], as_dict=True)
			if term
			else None
		)
		if bounds and bounds.term_start_date and getdate(bounds.term_start_date) <= now:
			start = start or getdate(bounds.term_start_date)
			end = end or min(getdate(bounds.term_end_date or now), now)
		else:
			end = end or now
			start = start or add_days(end, -30)

	start, end = getdate(start), getdate(end)
	if start > end:
		start, end = end, start
	if (end - start).days > MAX_SPAN_DAYS:
		start = add_days(end, -MAX_SPAN_DAYS)
	return start, end


def _period_info(start, end) -> dict:
	return {
		"dateFrom": str(start),
		"dateTo": str(end),
		"academicYear": get_default_academic_year(),
		"academicTerm": get_default_academic_term(),
	}


def _sections(program: str = None, student_group: str = None) -> list[dict]:
	"""The live sections of the selected period, with how many pupils each has."""
	filters: dict = {"disabled": 0}
	apply_period(filters, "Student Group")
	if program:
		filters["program"] = program
	if student_group:
		filters["name"] = student_group
	rows = frappe.get_all(
		"Student Group",
		filters=filters,
		fields=["name", "student_group_name", "program", "course", "ms_homeroom_instructor", "academic_term"],
		order_by="program asc, student_group_name asc",
		limit_page_length=0,
	)
	if not rows:
		return []
	counts = {
		r.parent: cint(r.n)
		for r in frappe.db.sql(
			"""select parent, count(*) as n from `tabStudent Group Student`
			where parenttype = 'Student Group' and active = 1 and parent in %(g)s
			group by parent""",
			{"g": [r.name for r in rows]},
			as_dict=True,
		)
	}
	return [
		{
			"id": r.name,
			"label": r.student_group_name or r.name,
			"program": r.program or "",
			"course": r.course or "",
			"homeroom": r.ms_homeroom_instructor or "",
			"term": r.academic_term or "",
			"students": counts.get(r.name, 0),
		}
		for r in rows
	]


def _teachers(teacher: str = None) -> dict[str, str]:
	"""Teachers still on staff, id → the name the school knows them by."""
	filters: dict = {"status": ["!=", "Left"]}
	if teacher:
		filters["name"] = teacher
	return {
		r.name: r.instructor_name or r.name
		for r in frappe.get_all(
			"Instructor", filters=filters, fields=["name", "instructor_name"], order_by="instructor_name",
			limit_page_length=0,
		)
	}


def _teacher_users(ids) -> dict[str, str]:
	"""Each teacher's login, for many teachers at once.

	The same three routes as `utils.instructor_user`, in the same order, but
	read in three queries for the whole staff instead of three per teacher.
	"""
	ids = list(ids)
	if not ids:
		return {}
	rows = frappe.get_all(
		"Instructor",
		filters={"name": ["in", ids]},
		fields=["name", "employee", "ms_user", "instructor_name"],
		limit_page_length=0,
	)
	employees = [r.employee for r in rows if r.employee]
	by_employee = (
		{
			r.name: r.user_id
			for r in frappe.get_all(
				"Employee", filters={"name": ["in", employees]}, fields=["name", "user_id"], limit_page_length=0
			)
			if r.user_id
		}
		if employees
		else {}
	)
	names = [r.instructor_name for r in rows if r.instructor_name and not r.ms_user]
	by_name: dict[str, str] = {}
	if names:
		for u in frappe.get_all(
			"User", filters={"full_name": ["in", names], "enabled": 1}, fields=["name", "full_name"],
			limit_page_length=0,
		):
			by_name.setdefault(u.full_name, u.name)

	out = {}
	for r in rows:
		user = by_employee.get(r.employee) or r.ms_user or by_name.get(r.instructor_name)
		if user:
			out[r.name] = user
	return out


def _teaching(sections: list[dict], teacher: str = None) -> dict[tuple, set]:
	"""(section, subject) → the teachers who teach it this period."""
	ids = [s["id"] for s in sections]
	pairs: dict[tuple, set] = {}
	if not ids:
		return pairs

	def add(group, course, instructor):
		if group and course and instructor and (not teacher or instructor == teacher):
			pairs.setdefault((group, course), set()).add(instructor)

	for r in frappe.db.sql(
		"""select distinct student_group, course, instructor from `tabCourse Schedule`
		where docstatus < 2 and student_group in %(g)s""",
		{"g": ids},
		as_dict=True,
	):
		add(r.student_group, r.course, r.instructor)

	if frappe.db.table_exists("MS Timetable Slot"):
		for r in frappe.db.sql(
			"""select distinct student_group, course, instructor from `tabMS Timetable Slot`
			where active = 1 and student_group in %(g)s""",
			{"g": ids},
			as_dict=True,
		):
			add(r.student_group, r.course, r.instructor)

	# The older one-subject section names its subject on itself and its
	# teacher in the instructors table.
	single = {s["id"]: s["course"] for s in sections if s["course"]}
	if single:
		for r in frappe.get_all(
			"Student Group Instructor",
			filters={"parent": ["in", list(single)], "parenttype": "Student Group"},
			fields=["parent", "instructor"],
			limit_page_length=0,
		):
			add(r.parent, single[r.parent], r.instructor)
	return pairs


def _school_days(start, end) -> list[datetime.date]:
	"""The days the school was open between two dates.

	Closed days come from the school's holiday list (the same one attendance
	refuses to be taken on). Many lists record only Friday as the weekly day
	off, so the school week is also bounded by the days the timetable runs —
	Sunday to Thursday when there is no timetable to ask.
	"""
	from match_schools.api.academic_context import holiday_reason

	week = set(DEFAULT_SCHOOL_DAYS)
	if frappe.db.table_exists("MS Timetable Slot"):
		week |= set(
			frappe.get_all("MS Timetable Slot", filters={"active": 1}, pluck="day", distinct=True, limit_page_length=0)
		)
	last = min(getdate(end), getdate(today()))
	out = []
	day = getdate(start)
	while day <= last:
		if WEEK[day.weekday()] in week and not holiday_reason(day):
			out.append(day)
		day = add_days(day, 1)
	return out


def _unknown_teacher() -> dict:
	return fail("Teacher not found.", "المعلم المحدد غير موجود أو لم يعد على رأس عمله.")


def _pct(part, whole) -> float | None:
	return round(flt(part) / flt(whole) * 100, 1) if whole else None


def _span(start, end) -> dict:
	"""Bounds for a datetime column: the whole of the last day included."""
	return {"d1": str(start), "d2x": str(add_days(end, 1)), "dend": str(end)}


# --- Options ----------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def options(persona: str = None):
	"""What the filter bar offers, and the default range it opens on."""
	start, end = _range()
	sections = _sections()
	programs = sorted({s["program"] for s in sections if s["program"]})
	return {
		**_period_info(start, end),
		"teachers": [{"id": k, "name": v} for k, v in _teachers().items()],
		"sections": [{"id": s["id"], "label": s["label"], "program": s["program"]} for s in sections],
		"programs": [{"id": p, "label": p} for p in programs],
	}


# --- 1. Marks entry ---------------------------------------------------------------


def _due_components(scheme: dict, quarters: dict, as_of) -> list[dict]:
	"""The plan's markable lines a teacher can be expected to have marked by now.

	A bonus line is optional by nature, and a line in a quarter that has not
	started cannot be late yet; a quarter with no dates counts as started,
	since the school has not said otherwise.
	"""
	out = []
	for c in scheme.get("components") or []:
		if c.get("component_type") == "Bonus":
			continue
		start = quarters.get(c.get("quarter") or "")
		if start and getdate(start) > getdate(as_of):
			continue
		out.append(c)
	return out


def _marks_rows(program=None, student_group=None, teacher=None, as_of=None) -> dict:
	from match_schools.api.gradebook import resolve_scheme

	as_of = getdate(as_of or today())
	sections = [s for s in _sections(program, student_group) if s["students"]]
	by_id = {s["id"]: s for s in sections}
	pairs = _teaching(sections, teacher)
	people = _teachers()
	year = get_default_academic_year()
	term = get_default_academic_term()

	quarters = {}
	if term:
		quarters = {
			q.quarter_name: q.from_date
			for q in frappe.get_all(
				"MS Term Quarter",
				filters={"parent": term, "parenttype": "Academic Term"},
				fields=["quarter_name", "from_date"],
			)
		}

	# How many current pupils have a mark in each column, per section and
	# subject. Joined to the live roster so a pupil who has left neither
	# counts towards a column nor makes it look more finished than it is.
	filled: dict[tuple, dict[str, int]] = {}
	last: dict[tuple, str] = {}
	any_marks: dict[tuple, int] = {}
	if pairs:
		params = {"g": sorted({g for g, _c in pairs}), "y": year, "t": term or ""}
		for r in frappe.db.sql(
			"""select e.student_group, e.course, e.component_name,
				count(distinct e.student) as n, max(e.modified) as last
			from `tabMS Gradebook Entry` e
			join `tabStudent Group Student` s
				on s.parent = e.student_group and s.parenttype = 'Student Group'
				and s.student = e.student and s.active = 1
			where e.student_group in %(g)s
				and (%(y)s is null or e.academic_year = %(y)s)
				and ifnull(e.academic_term, '') in (%(t)s, '')
			group by e.student_group, e.course, e.component_name""",
			params,
			as_dict=True,
		):
			key = (r.student_group, r.course)
			filled.setdefault(key, {})[r.component_name or ""] = cint(r.n)
			any_marks[key] = any_marks.get(key, 0) + cint(r.n)
			stamp = str(r.last or "")[:16]
			if stamp > last.get(key, ""):
				last[key] = stamp

	schemes: dict[tuple, dict | None] = {}
	rows = []
	for (group, course), teachers in pairs.items():
		section = by_id[group]
		skey = (course, section["program"])
		if skey not in schemes:
			schemes[skey] = resolve_scheme(course, section["program"] or None)
		scheme = schemes[skey]
		students = section["students"]
		cols = filled.get((group, course), {})

		if scheme:
			due = _due_components(scheme, quarters, as_of)
			expected = students * len(due)
			entered = sum(min(cols.get(c["component_name"], 0), students) for c in due)
			done = sum(1 for c in due if cols.get(c["component_name"], 0) >= students)
			empty = sum(1 for c in due if not cols.get(c["component_name"]))
			missing_cols = [c["component_name"] for c in due if cols.get(c["component_name"], 0) < students]
		else:
			due, expected, entered, done, empty, missing_cols = [], 0, 0, 0, 0, []

		total_marks = any_marks.get((group, course), 0)
		if not total_marks:
			status = "none"
		elif not scheme or not due:
			status = "noPlan"
		elif entered >= expected:
			status = "complete"
		else:
			status = "partial"

		for t in sorted(teachers):
			rows.append(
				{
					"teacher": t,
					"teacherName": people.get(t, t),
					"section": group,
					"sectionLabel": section["label"],
					"program": section["program"],
					"course": course,
					"students": students,
					"plan": scheme["scheme_name"] if scheme else None,
					"components": len(due),
					"columnsDone": done,
					"columnsEmpty": empty,
					"missingColumns": missing_cols[:12],
					"expected": expected,
					"entered": entered,
					"missing": max(expected - entered, 0),
					"marksTotal": total_marks,
					"pct": _pct(entered, expected) if expected else (100.0 if total_marks else 0.0),
					"lastEntry": last.get((group, course)) or None,
					"status": status,
				}
			)

	rows.sort(key=lambda r: (r["pct"] if r["pct"] is not None else 0, r["teacherName"], r["sectionLabel"]))

	by_teacher: dict[str, dict] = {}
	for r in rows:
		t = by_teacher.setdefault(
			r["teacher"],
			{
				"teacher": r["teacher"],
				"teacherName": r["teacherName"],
				"pairs": 0, "pairsNone": 0, "pairsPartial": 0, "pairsComplete": 0, "pairsNoPlan": 0,
				"expected": 0, "entered": 0, "missing": 0, "marksTotal": 0,
				"lastEntry": None, "notEntered": [],
			},
		)
		t["pairs"] += 1
		t["pairs" + r["status"][0].upper() + r["status"][1:]] += 1
		t["expected"] += r["expected"]
		t["entered"] += r["entered"]
		t["missing"] += r["missing"]
		t["marksTotal"] += r["marksTotal"]
		if r["lastEntry"] and (not t["lastEntry"] or r["lastEntry"] > t["lastEntry"]):
			t["lastEntry"] = r["lastEntry"]
		if r["status"] == "none":
			t["notEntered"].append(f"{r['sectionLabel']} — {r['course']}")

	teachers = []
	for t in by_teacher.values():
		t["pct"] = _pct(t["entered"], t["expected"]) if t["expected"] else (100.0 if t["marksTotal"] else 0.0)
		# Nothing at all in any of their classes is the answer to «مين مش
		# مدخل علاماته»; a few gaps is a different conversation.
		t["state"] = "none" if not t["marksTotal"] else ("complete" if not t["missing"] and not t["pairsNone"] else "partial")
		teachers.append(t)
	teachers.sort(key=lambda t: (t["pct"], -t["missing"], t["teacherName"]))

	expected = sum(r["expected"] for r in rows)
	entered = sum(r["entered"] for r in rows)
	return {
		"asOf": str(as_of),
		"summary": {
			"teachers": len(teachers),
			"teachersNoMarks": sum(1 for t in teachers if t["state"] == "none"),
			"teachersWithMissing": sum(1 for t in teachers if t["state"] != "complete"),
			"teachersComplete": sum(1 for t in teachers if t["state"] == "complete"),
			"pairs": len(rows),
			"pairsNone": sum(1 for r in rows if r["status"] == "none"),
			"pairsNoPlan": sum(1 for r in rows if r["status"] == "noPlan"),
			"expected": expected,
			"entered": entered,
			"missing": max(expected - entered, 0),
			"pct": _pct(entered, expected),
		},
		"teachers": teachers,
		"rows": rows,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def marks_completion(
	date_from: str = None,
	date_to: str = None,
	program: str = None,
	student_group: str = None,
	teacher: str = None,
	persona: str = None,
):
	"""Marks entered against the assessment plan, per teacher and per class.

	Marks belong to the term rather than to a date range, so the range's end
	only decides which quarters are due: a line in a quarter that starts next
	month is not yet missing.
	"""
	start, end = _range(date_from, date_to)
	if teacher and not _teachers(teacher):
		return _unknown_teacher()
	data = _marks_rows(program, student_group, teacher, as_of=end)
	return {**_period_info(start, end), **data}


# --- 2. Teacher activity ----------------------------------------------------------


def _grouped(sql: str, params: dict) -> list:
	return frappe.db.sql(sql, params, as_dict=True)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def teacher_activity(
	date_from: str = None, date_to: str = None, teacher: str = None, program: str = None,
	student_group: str = None, persona: str = None,
):
	"""What each teacher did on the system over a period, and when they were last on it.

	Counted from the records their work leaves behind, not from page views:
	a register taken, a lesson logged, a mark entered. A teacher with none of
	those in a month has not been using the system, whatever their last login.
	"""
	start, end = _range(date_from, date_to)
	people = _teachers(teacher)
	if teacher and not people:
		return _unknown_teacher()
	users = _teacher_users(people)
	by_user = {u: t for t, u in users.items()}
	p = {**_span(start, end), "users": list(by_user) or [""], "ids": list(people) or [""]}

	counts: dict[str, dict[str, int]] = {t: {} for t in people}
	days: dict[str, set] = {t: set() for t in people}
	daily: dict[str, int] = {}

	def tally(rows, key_field: str, metric: str, via_user: bool, value_field: str = "n"):
		for r in rows:
			who = by_user.get(r[key_field]) if via_user else r[key_field]
			if who not in counts:
				continue
			n = cint(r[value_field])
			counts[who][metric] = counts[who].get(metric, 0) + n
			if r.get("d"):
				days[who].add(str(r["d"]))
				daily[str(r["d"])] = daily.get(str(r["d"]), 0) + n

	# Registers: one per section per day, however many pupils were on it.
	tally(
		_grouped(
			"""select owner as k, date as d, count(distinct student_group) as n, count(*) as recs
			from `tabStudent Attendance`
			where docstatus < 2 and date between %(d1)s and %(dend)s and owner in %(users)s
			group by owner, date""",
			p,
		),
		"k", "attendanceSessions", True,
	)
	recs: dict[str, int] = {}
	for r in frappe.db.sql(
		"""select owner as k, count(*) as n from `tabStudent Attendance`
		where docstatus < 2 and date between %(d1)s and %(dend)s and owner in %(users)s group by owner""",
		p,
		as_dict=True,
	):
		if by_user.get(r.k):
			recs[by_user[r.k]] = cint(r.n)

	tally(
		_grouped(
			"""select instructor as k, log_date as d, count(*) as n from `tabMS Class Log`
			where log_date between %(d1)s and %(dend)s and instructor in %(ids)s
			group by instructor, log_date""",
			p,
		),
		"k", "classLogs", False,
	)
	tally(
		_grouped(
			"""select instructor as k, date(ifnull(prepared_on, creation)) as d, count(*) as n
			from `tabMS Lesson Plan`
			where ifnull(prepared_on, creation) >= %(d1)s and ifnull(prepared_on, creation) < %(d2x)s
				and instructor in %(ids)s
			group by instructor, date(ifnull(prepared_on, creation))""",
			p,
		),
		"k", "lessonPlans", False,
	)
	tally(
		_grouped(
			"""select ifnull(nullif(entered_by, ''), owner) as k, date(creation) as d, count(*) as n
			from `tabMS Gradebook Entry`
			where creation >= %(d1)s and creation < %(d2x)s
				and ifnull(nullif(entered_by, ''), owner) in %(users)s
			group by ifnull(nullif(entered_by, ''), owner), date(creation)""",
			p,
		),
		"k", "marks", True,
	)
	tally(
		_grouped(
			"""select instructor as k, date(creation) as d, count(*) as n from `tabMS Assignment`
			where creation >= %(d1)s and creation < %(d2x)s and instructor in %(ids)s
			group by instructor, date(creation)""",
			p,
		),
		"k", "assignments", False,
	)
	tally(
		_grouped(
			"""select reported_by as k, record_date as d, count(*) as n from `tabMS Behaviour Record`
			where record_date between %(d1)s and %(dend)s and reported_by in %(ids)s
			group by reported_by, record_date""",
			p,
		),
		"k", "behaviour", False,
	)
	# A message to a whole class is one thread, not thirty messages.
	tally(
		_grouped(
			"""select sender as k, date(sent_on) as d, count(distinct ifnull(nullif(thread, ''), name)) as n
			from `tabMS Message`
			where ifnull(is_draft, 0) = 0 and sent_on >= %(d1)s and sent_on < %(d2x)s and sender in %(users)s
			group by sender, date(sent_on)""",
			p,
		),
		"k", "messages", True,
	)
	if frappe.db.table_exists("MS Periodic Entry"):
		tally(
			_grouped(
				"""select evaluated_by as k, date(evaluated_on) as d, count(*) as n from `tabMS Periodic Entry`
				where evaluated_on >= %(d1)s and evaluated_on < %(d2x)s and evaluated_by in %(users)s
				group by evaluated_by, date(evaluated_on)""",
				p,
			),
			"k", "evaluations", True,
		)

	logins = {
		u.name: u
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", list(by_user) or [""]]},
			fields=["name", "enabled", "last_login", "last_active"],
			limit_page_length=0,
		)
	}

	metrics = ["attendanceSessions", "classLogs", "lessonPlans", "marks", "assignments", "behaviour", "messages", "evaluations"]
	rows = []
	for t, name in people.items():
		c = counts[t]
		user = users.get(t)
		u = logins.get(user) if user else None
		row = {"teacher": t, "teacherName": name, "user": user or None}
		for m in metrics:
			row[m] = c.get(m, 0)
		row["attendanceRecords"] = recs.get(t, 0)
		row["total"] = sum(c.get(m, 0) for m in metrics)
		row["activeDays"] = len(days[t])
		row["lastLogin"] = str(u.last_login)[:16] if u and u.last_login else None
		row["lastActive"] = str(u.last_active)[:16] if u and u.last_active else None
		row["accountEnabled"] = bool(u and cint(u.enabled))
		row["state"] = "noAccount" if not user else ("inactive" if not row["total"] else "active")
		rows.append(row)
	rows.sort(key=lambda r: (-r["total"], r["teacherName"]))

	school_days = _school_days(start, end)
	series = [{"date": str(d), "label": str(d)[5:], "actions": daily.get(str(d), 0)} for d in school_days]

	return {
		**_period_info(start, end),
		"summary": {
			"teachers": len(rows),
			"active": sum(1 for r in rows if r["state"] == "active"),
			"inactive": sum(1 for r in rows if r["state"] == "inactive"),
			"noAccount": sum(1 for r in rows if r["state"] == "noAccount"),
			"neverLoggedIn": sum(1 for r in rows if r["user"] and not r["lastLogin"]),
			"schoolDays": len(school_days),
			**{m: sum(r[m] for r in rows) for m in metrics},
		},
		"rows": rows,
		"daily": series,
	}


# --- 3. Duty and cover ------------------------------------------------------------


def _duty_load(people: dict[str, str]) -> tuple[dict[str, int], dict[str, set], list[dict]]:
	"""Duties a week per teacher from the active rotas of this year.

	A rota row may name someone typed by hand rather than picked from the
	list; those are matched to a teacher by name, and kept as their own row
	when they match no one, so a duty is never silently dropped.
	"""
	from match_schools.api.duty_schedule import _entries

	year = get_default_academic_year()
	everyone = {
		r.name: r.instructor_name or r.name
		for r in frappe.get_all("Instructor", filters={"status": ["!=", "Left"]}, fields=["name", "instructor_name"])
	}
	name_to_id = {v.strip(): k for k, v in everyone.items()}
	load: dict[str, int] = {}
	places: dict[str, set] = {}
	unmatched: dict[str, int] = {}
	programs = frappe.get_all(
		"MS Duty Program",
		filters={"is_active": 1},
		fields=["name", "title", "academic_year", "entries"],
		limit_page_length=0,
	)
	for prog in programs:
		if year and prog.academic_year and prog.academic_year != year:
			continue
		for e in _entries(prog.entries, everyone):
			who = e["teacher"] or name_to_id.get(e["name"].strip())
			if who:
				load[who] = load.get(who, 0) + 1
				if e["place"]:
					places.setdefault(who, set()).add(e["place"])
			else:
				unmatched[e["name"]] = unmatched.get(e["name"], 0) + 1
	return load, places, [{"name": k, "count": v} for k, v in sorted(unmatched.items(), key=lambda x: -x[1])]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def duty_cover(
	date_from: str = None, date_to: str = None, teacher: str = None, program: str = None,
	student_group: str = None, persona: str = None,
):
	"""المناوبات والإشغالات: who carries the covers, the duties and the teaching load.

	A cover is a lesson recorded as taken by a substitute (MS Lesson Change,
	submitted). The saved «جدول الإشغال» tables list who was free, not who
	covered, so they say nothing about the load and are not counted.
	"""
	start, end = _range(date_from, date_to)
	people = _teachers(teacher)
	if teacher and not people:
		return _unknown_teacher()
	p = {"d1": str(start), "dend": str(end)}

	covers: dict[str, int] = {}
	covered_for: dict[str, int] = {}
	by_reason: dict[str, int] = {}
	by_day: dict[str, int] = {}
	swaps: dict[str, int] = {}
	swap_count = 0
	for r in frappe.db.sql(
		"""select instructor, original_instructor, change_type, ifnull(reason, '') as reason,
			schedule_date from `tabMS Lesson Change`
		where docstatus = 1 and schedule_date between %(d1)s and %(dend)s
			and change_type in ('Substitute', 'Swap')""",
		p,
		as_dict=True,
	):
		if r.change_type == "Swap":
			swap_count += 1
			for who in {r.instructor, r.original_instructor}:
				if who:
					swaps[who] = swaps.get(who, 0) + 1
			continue
		if r.instructor:
			covers[r.instructor] = covers.get(r.instructor, 0) + 1
		if r.original_instructor:
			covered_for[r.original_instructor] = covered_for.get(r.original_instructor, 0) + 1
		label = REASON_AR.get(r.reason, r.reason)
		by_reason[label] = by_reason.get(label, 0) + 1
		d = str(r.schedule_date)
		by_day[d] = by_day.get(d, 0) + 1

	sections = _sections()
	ids = [s["id"] for s in sections]
	week_load: dict[str, int] = {}
	week_sections: dict[str, set] = {}
	week_subjects: dict[str, set] = {}
	if ids and frappe.db.table_exists("MS Timetable Slot"):
		for r in frappe.db.sql(
			"""select instructor, student_group, course from `tabMS Timetable Slot`
			where active = 1 and student_group in %(g)s and ifnull(instructor, '') != ''""",
			{"g": ids},
			as_dict=True,
		):
			week_load[r.instructor] = week_load.get(r.instructor, 0) + 1
			week_sections.setdefault(r.instructor, set()).add(r.student_group)
			if r.course:
				week_subjects.setdefault(r.instructor, set()).add(r.course)

	# The lessons actually on the calendar in the range — the timetable as it
	# was lived, after any lesson generated or deleted by hand.
	lessons: dict[str, int] = {}
	for r in frappe.db.sql(
		"""select instructor, count(*) as n from `tabCourse Schedule`
		where docstatus < 2 and schedule_date between %(d1)s and %(dend)s and ifnull(instructor, '') != ''
		group by instructor""",
		p,
		as_dict=True,
	):
		lessons[r.instructor] = cint(r.n)

	duties, places, unmatched = _duty_load(people)

	rows = []
	for t, name in people.items():
		row = {
			"teacher": t,
			"teacherName": name,
			"weeklyLessons": week_load.get(t, 0),
			"sections": len(week_sections.get(t, ())),
			"subjects": len(week_subjects.get(t, ())),
			"weeklyDuties": duties.get(t, 0),
			"dutyPlaces": sorted(places.get(t, ())),
			"covers": covers.get(t, 0),
			"coveredFor": covered_for.get(t, 0),
			"swaps": swaps.get(t, 0),
			"scheduledLessons": lessons.get(t, 0),
		}
		row["weeklyTotal"] = row["weeklyLessons"] + row["weeklyDuties"]
		rows.append(row)
	rows.sort(key=lambda r: (-(r["weeklyTotal"] + r["covers"]), r["teacherName"]))

	with_load = [r for r in rows if r["weeklyLessons"]]
	return {
		**_period_info(start, end),
		"summary": {
			"teachers": len(rows),
			"covers": sum(covers.values()),
			"swaps": swap_count,
			"weeklyDuties": sum(duties.values()),
			"weeklyLessons": sum(r["weeklyLessons"] for r in rows),
			"avgWeeklyLessons": round(sum(r["weeklyLessons"] for r in with_load) / len(with_load), 1) if with_load else 0,
			"teachersWithoutDuty": sum(1 for r in rows if not r["weeklyDuties"]),
			"teachersWithoutCover": sum(1 for r in rows if not r["covers"]),
		},
		"rows": rows,
		"byReason": [{"label": k, "count": v} for k, v in sorted(by_reason.items(), key=lambda x: -x[1])],
		"daily": [{"date": d, "label": d[5:], "covers": n} for d, n in sorted(by_day.items())],
		"unmatchedDuties": unmatched,
	}


# --- 4. Attendance taking ---------------------------------------------------------


def _attendance_rows(start, end, program=None, student_group=None) -> dict:
	sections = [s for s in _sections(program, student_group) if s["students"]]
	days = _school_days(start, end)
	people = _teachers()
	taken: dict[str, set] = {}
	if sections and days:
		for r in frappe.db.sql(
			"""select distinct student_group, date from `tabStudent Attendance`
			where docstatus < 2 and date between %(d1)s and %(dend)s and student_group in %(g)s""",
			{"d1": str(start), "dend": str(end), "g": [s["id"] for s in sections]},
			as_dict=True,
		):
			taken.setdefault(r.student_group, set()).add(str(r.date))

	day_keys = [str(d) for d in days]
	rows = []
	per_day = {d: 0 for d in day_keys}
	for s in sections:
		mine = taken.get(s["id"], set())
		done = [d for d in day_keys if d in mine]
		missing = [d for d in day_keys if d not in mine]
		for d in done:
			per_day[d] += 1
		rows.append(
			{
				"section": s["id"],
				"sectionLabel": s["label"],
				"program": s["program"],
				"homeroom": people.get(s["homeroom"], s["homeroom"]) if s["homeroom"] else "",
				"students": s["students"],
				"schoolDays": len(day_keys),
				"taken": len(done),
				"missing": len(missing),
				"pct": _pct(len(done), len(day_keys)),
				"missingDates": missing[-MAX_LISTED_DAYS:],
				"lastTaken": max(mine) if mine else None,
			}
		)
	rows.sort(key=lambda r: (r["pct"] if r["pct"] is not None else 0, r["sectionLabel"]))

	expected = len(sections) * len(day_keys)
	done_total = sum(r["taken"] for r in rows)
	daily = [
		{
			"date": d,
			"label": d[5:],
			"weekday": DAY_AR.get(WEEK[getdate(d).weekday()], ""),
			"taken": per_day[d],
			"missing": len(sections) - per_day[d],
			"pct": _pct(per_day[d], len(sections)),
		}
		for d in day_keys
	]
	return {
		"summary": {
			"sections": len(sections),
			"schoolDays": len(day_keys),
			"expected": expected,
			"taken": done_total,
			"missing": expected - done_total,
			"pct": _pct(done_total, expected),
			"sectionsComplete": sum(1 for r in rows if not r["missing"]),
			"sectionsNever": sum(1 for r in rows if not r["taken"]),
			"daysWithGaps": sum(1 for d in daily if d["missing"]),
		},
		"rows": rows,
		"daily": daily,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def attendance_compliance(
	date_from: str = None, date_to: str = None, program: str = None, student_group: str = None,
	teacher: str = None, persona: str = None,
):
	"""School days on which a section's register was never taken.

	A register counts as taken when at least one pupil of the section was
	marked that day — the class teacher marks the whole list in one go, so a
	day with any mark is a day someone opened it.
	"""
	start, end = _range(date_from, date_to)
	return {**_period_info(start, end), **_attendance_rows(start, end, program, student_group)}


# --- 5. Lesson logs and preparation -----------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def class_log_compliance(
	date_from: str = None, date_to: str = None, teacher: str = None, program: str = None,
	student_group: str = None, persona: str = None,
):
	"""Lessons that happened against the logs and lesson plans written for them.

	A lesson is matched by section, subject and day: a log for «الرياضيات،
	السابع أ، الأحد» answers the lesson that ran then, whichever period it
	was. A lesson covered by a substitute belongs to whoever covered it, and a
	cancelled lesson is not expected to have a log at all.

	Schools that never generate the term's lessons into Course Schedule still
	get an answer: the weekly timetable is laid over the school days instead.
	"""
	start, end = _range(date_from, date_to)
	end = min(end, getdate(today()))
	sections = _sections(program, student_group)
	ids = [s["id"] for s in sections]
	people = _teachers()
	p = {"d1": str(start), "dend": str(end), "g": ids or [""]}

	changes = {
		r.course_schedule: r
		for r in frappe.db.sql(
			"""select course_schedule, change_type, instructor from `tabMS Lesson Change`
			where docstatus = 1 and schedule_date between %(d1)s and %(dend)s
				and ifnull(course_schedule, '') != '' and change_type in ('Substitute', 'Cancelled')""",
			p,
			as_dict=True,
		)
	}

	lessons = frappe.db.sql(
		"""select name, student_group, course, instructor, schedule_date from `tabCourse Schedule`
		where docstatus < 2 and schedule_date between %(d1)s and %(dend)s and student_group in %(g)s
			and ifnull(instructor, '') != ''""",
		p,
		as_dict=True,
	)
	source = "lessons"
	expected: list[tuple] = []
	for r in lessons:
		ch = changes.get(r.name)
		if ch and ch.change_type == "Cancelled":
			continue
		who = ch.instructor if ch and ch.change_type == "Substitute" and ch.instructor else r.instructor
		expected.append((who, r.student_group, r.course, str(r.schedule_date)))

	if not lessons and ids and frappe.db.table_exists("MS Timetable Slot"):
		source = "timetable"
		slots = frappe.db.sql(
			"""select day, student_group, course, instructor from `tabMS Timetable Slot`
			where active = 1 and student_group in %(g)s and ifnull(instructor, '') != ''""",
			p,
			as_dict=True,
		)
		by_day: dict[str, list] = {}
		for s in slots:
			by_day.setdefault(s.day, []).append(s)
		for d in _school_days(start, end):
			for s in by_day.get(WEEK[d.weekday()], []):
				expected.append((s.instructor, s.student_group, s.course, str(d)))

	if teacher:
		expected = [e for e in expected if e[0] == teacher]

	def available(sql: str) -> dict[tuple, int]:
		out: dict[tuple, int] = {}
		for r in frappe.db.sql(sql, p, as_dict=True):
			key = (r.student_group, r.course, str(r.d))
			out[key] = out.get(key, 0) + cint(r.n)
		return out

	logs = available(
		"""select student_group, course, log_date as d, count(*) as n from `tabMS Class Log`
		where log_date between %(d1)s and %(dend)s and student_group in %(g)s
		group by student_group, course, log_date"""
	)
	plans = available(
		"""select student_group, course, schedule_date as d, count(*) as n from `tabMS Lesson Plan`
		where schedule_date between %(d1)s and %(dend)s and student_group in %(g)s
		group by student_group, course, schedule_date"""
	)

	rows_by: dict[str, dict] = {}
	daily: dict[str, dict] = {}
	used_logs: dict[tuple, int] = {}
	used_plans: dict[tuple, int] = {}
	for who, group, course, d in sorted(expected, key=lambda e: e[3]):
		key = (group, course, d)
		row = rows_by.setdefault(
			who,
			{"teacher": who, "teacherName": people.get(who, who), "lessons": 0, "logged": 0, "planned": 0, "lastLog": None},
		)
		day = daily.setdefault(d, {"date": d, "label": d[5:], "lessons": 0, "logged": 0, "planned": 0})
		row["lessons"] += 1
		day["lessons"] += 1
		# Two lessons of the same subject on one day need two logs.
		if used_logs.get(key, 0) < logs.get(key, 0):
			used_logs[key] = used_logs.get(key, 0) + 1
			row["logged"] += 1
			day["logged"] += 1
			row["lastLog"] = d
		if used_plans.get(key, 0) < plans.get(key, 0):
			used_plans[key] = used_plans.get(key, 0) + 1
			row["planned"] += 1
			day["planned"] += 1

	rows = []
	for r in rows_by.values():
		r["missingLogs"] = r["lessons"] - r["logged"]
		r["missingPlans"] = r["lessons"] - r["planned"]
		r["logPct"] = _pct(r["logged"], r["lessons"])
		r["planPct"] = _pct(r["planned"], r["lessons"])
		rows.append(r)
	rows.sort(key=lambda r: (r["logPct"] or 0, r["teacherName"]))

	total = sum(r["lessons"] for r in rows)
	logged = sum(r["logged"] for r in rows)
	planned = sum(r["planned"] for r in rows)
	return {
		**_period_info(start, end),
		"source": source,
		"summary": {
			"teachers": len(rows),
			"lessons": total,
			"logged": logged,
			"planned": planned,
			"missingLogs": total - logged,
			"missingPlans": total - planned,
			"logPct": _pct(logged, total),
			"planPct": _pct(planned, total),
			"teachersNoLogs": sum(1 for r in rows if not r["logged"]),
		},
		"rows": rows,
		"daily": [
			{**d, "logPct": _pct(d["logged"], d["lessons"])} for d in sorted(daily.values(), key=lambda x: x["date"])
		],
	}


# --- 6. Sections at a glance ------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def section_overview(
	date_from: str = None, date_to: str = None, program: str = None, student_group: str = None,
	teacher: str = None, persona: str = None,
):
	"""Each section on one line: size, absence, behaviour, marks, exams, clinic visits."""
	start, end = _range(date_from, date_to)
	sections = _sections(program, student_group)
	people = _teachers()
	ids = [s["id"] for s in sections]
	if not ids:
		return {**_period_info(start, end), "summary": {"sections": 0}, "rows": [], "byProgram": []}
	p = {**_span(start, end), "g": ids, "y": get_default_academic_year(), "t": get_default_academic_term() or ""}

	att = {
		r.student_group: r
		for r in frappe.db.sql(
			"""select student_group,
				sum(status = 'Present') as present, sum(status = 'Absent') as absent,
				sum(status in ('Excused', 'Leave')) as excused, count(distinct date) as days
			from `tabStudent Attendance`
			where docstatus < 2 and date between %(d1)s and %(dend)s and student_group in %(g)s
			group by student_group""",
			p,
			as_dict=True,
		)
	}
	beh = {
		r.student_group: r
		for r in frappe.db.sql(
			# Most records are written from the pupil's page and carry no
			# section, so those count towards the section the pupil sits in.
			"""select ifnull(nullif(b.student_group, ''), s.parent) as student_group,
				sum(b.record_type = 'Negative') as neg, sum(b.record_type = 'Positive') as pos
			from `tabMS Behaviour Record` b
			left join `tabStudent Group Student` s
				on ifnull(b.student_group, '') = '' and s.student = b.student
				and s.parenttype = 'Student Group' and s.active = 1 and s.parent in %(g)s
			where b.record_date between %(d1)s and %(dend)s
				and ifnull(nullif(b.student_group, ''), s.parent) in %(g)s
			group by ifnull(nullif(b.student_group, ''), s.parent)""",
			p,
			as_dict=True,
		)
	}
	# Marks belong to the term, not the range: the average is the term's so far.
	marks = {
		r.student_group: r
		for r in frappe.db.sql(
			"""select student_group, avg(score / max_score * 100) as avg_pct, count(*) as n
			from `tabMS Gradebook Entry`
			where student_group in %(g)s and max_score > 0 and ifnull(is_bonus, 0) = 0
				and (%(y)s is null or academic_year = %(y)s)
				and ifnull(academic_term, '') in (%(t)s, '')
			group by student_group""",
			p,
			as_dict=True,
		)
	}
	exams = {
		r.student_group: cint(r.n)
		for r in frappe.db.sql(
			"""select student_group, count(*) as n from `tabAssessment Plan`
			where docstatus < 2 and schedule_date between %(d1)s and %(dend)s and student_group in %(g)s
			group by student_group""",
			p,
			as_dict=True,
		)
	}
	# Clinic visits land on the section the pupil sits in now.
	visits = {
		r.parent: cint(r.n)
		for r in frappe.db.sql(
			"""select s.parent, count(*) as n
			from `tabMS Health Visit` v
			join `tabStudent Group Student` s
				on s.student = v.student and s.parenttype = 'Student Group' and s.active = 1
			where v.visit_date >= %(d1)s and v.visit_date < %(d2x)s and s.parent in %(g)s
			group by s.parent""",
			p,
			as_dict=True,
		)
	}

	rows = []
	for s in sections:
		a = att.get(s["id"]) or {}
		b = beh.get(s["id"]) or {}
		m = marks.get(s["id"]) or {}
		present, absent = cint(a.get("present")), cint(a.get("absent"))
		rows.append(
			{
				"section": s["id"],
				"sectionLabel": s["label"],
				"program": s["program"],
				"homeroom": people.get(s["homeroom"], s["homeroom"]) if s["homeroom"] else "",
				"students": s["students"],
				"attendanceDays": cint(a.get("days")),
				"present": present,
				"absent": absent,
				"excused": cint(a.get("excused")),
				"absenceRate": _pct(absent, present + absent) or 0.0,
				"negative": cint(b.get("neg")),
				"positive": cint(b.get("pos")),
				"negativePerStudent": round(cint(b.get("neg")) / s["students"], 2) if s["students"] else 0,
				"marksAverage": round(flt(m.get("avg_pct")), 1) if m.get("n") else None,
				"marks": cint(m.get("n")),
				"exams": exams.get(s["id"], 0),
				"visits": visits.get(s["id"], 0),
			}
		)

	programs: dict[str, dict] = {}
	for r in rows:
		g = programs.setdefault(
			r["program"] or "—",
			{"program": r["program"] or "—", "sections": 0, "students": 0, "present": 0, "absent": 0,
			 "negative": 0, "positive": 0, "visits": 0, "_marks": [], "exams": 0},
		)
		g["sections"] += 1
		for k in ("students", "present", "absent", "negative", "positive", "visits", "exams"):
			g[k] += r[k]
		if r["marksAverage"] is not None:
			g["_marks"].append(r["marksAverage"])
	by_program = []
	for g in programs.values():
		marks_list = g.pop("_marks")
		g["absenceRate"] = _pct(g["absent"], g["present"] + g["absent"]) or 0.0
		g["marksAverage"] = round(sum(marks_list) / len(marks_list), 1) if marks_list else None
		by_program.append(g)

	present = sum(r["present"] for r in rows)
	absent = sum(r["absent"] for r in rows)
	averaged = [r["marksAverage"] for r in rows if r["marksAverage"] is not None]
	return {
		**_period_info(start, end),
		"summary": {
			"sections": len(rows),
			"students": sum(r["students"] for r in rows),
			"absenceRate": _pct(absent, present + absent) or 0.0,
			"negative": sum(r["negative"] for r in rows),
			"positive": sum(r["positive"] for r in rows),
			"marksAverage": round(sum(averaged) / len(averaged), 1) if averaged else None,
			"exams": sum(r["exams"] for r in rows),
			"visits": sum(r["visits"] for r in rows),
		},
		"rows": rows,
		"byProgram": by_program,
	}


# --- The dashboard card -----------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def summary(date_from: str = None, date_to: str = None, persona: str = None):
	"""The few numbers a leadership dashboard opens on, and who needs a word."""
	start, end = _range(date_from, date_to)
	marks = _marks_rows(as_of=end)
	att = _attendance_rows(start, end)
	worst_marks = [
		{"id": t["teacher"], "name": t["teacherName"], "pct": t["pct"], "missing": t["missing"], "state": t["state"]}
		for t in marks["teachers"]
		if t["state"] != "complete"
	][:3]
	worst_att = [
		{"id": r["section"], "name": r["sectionLabel"], "pct": r["pct"], "missing": r["missing"]}
		for r in att["rows"]
		if r["missing"]
	][:3]
	return {
		**_period_info(start, end),
		"marks": marks["summary"],
		"attendance": att["summary"],
		"topMissingMarks": worst_marks,
		"topMissingAttendance": worst_att,
	}
