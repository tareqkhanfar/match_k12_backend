# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""تقارير التمريض — what the school nurse reads about the clinic.

Two sources: the visits (MS Health Visit — one row each time a pupil comes to
the clinic) and the health register (MS Health Record — one per pupil, with
the conditions, allergies and medicines the nurse must know about before
anything happens).

Read-only, and grouped in SQL or in one pass in Python: a busy clinic sees a
few thousand visits a year, and none of these screens should query once per
pupil. A visit is placed in the section the pupil sits in now; a pupil who
has since left the school keeps their visits in the totals under «—».
"""

import datetime
import re

import frappe
from frappe.utils import add_days, cint, getdate, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_SECRETARY,
	apply_period,
	fail,
	get_default_academic_term,
	ms_endpoint,
)
from match_schools.api.wellbeing import OUTCOME_AR, VISIT_TYPE_AR

NURSING = (ROLE_ADMIN, ROLE_SECRETARY)
WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_AR = {
	"Sunday": "الأحد", "Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء",
	"Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت",
}
MONTH_AR = [
	"كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران",
	"تموز", "آب", "أيلول", "تشرين الأول", "تشرين الثاني", "كانون الأول",
]
MAX_SPAN_DAYS = 400
# A day-by-day series longer than this is drawn by week instead: ninety bars
# on a phone are a grey smear.
MAX_DAILY_POINTS = 92
# Free-text lists ("ربو، حساسية القمح") are split on these.
SPLIT = re.compile(r"[,،;؛\n/]+")
# What a nurse writes for "none" — not a condition.
NOTHING = {"لا", "لا يوجد", "لايوجد", "لا شيء", "none", "no", "nil", "-", "—", "n/a", "na"}


# --- Period and pupils ------------------------------------------------------------


def _range(date_from=None, date_to=None) -> tuple[datetime.date, datetime.date]:
	"""The selected term so far, or the last thirty days when there is no term."""
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


def _placement() -> dict[str, dict]:
	"""Each pupil's current section and grade, in one query."""
	filters: dict = {"disabled": 0}
	apply_period(filters, "Student Group")
	groups = {
		g.name: g
		for g in frappe.get_all(
			"Student Group", filters=filters, fields=["name", "student_group_name", "program"], limit_page_length=0
		)
	}
	if not groups:
		return {}
	out: dict[str, dict] = {}
	for r in frappe.db.sql(
		"""select student, parent from `tabStudent Group Student`
		where parenttype = 'Student Group' and active = 1 and parent in %(g)s
		order by parent""",
		{"g": list(groups)},
		as_dict=True,
	):
		g = groups[r.parent]
		out.setdefault(
			r.student,
			{"section": g.name, "sectionLabel": g.student_group_name or g.name, "program": g.program or ""},
		)
	return out


def _in_scope(place: dict | None, program: str | None, student_group: str | None) -> bool:
	if not (program or student_group):
		return True
	if not place:
		return False
	if student_group and place["section"] != student_group:
		return False
	if program and place["program"] != program:
		return False
	return True


def _items(value) -> list[str]:
	"""A free-text field as its separate entries, "none" left out."""
	out = []
	for part in SPLIT.split(str(value or "")):
		text = re.sub(r"\s+", " ", part).strip(" .-")
		if text and text.lower() not in NOTHING:
			out.append(text)
	return out


def _has(value) -> bool:
	return bool(_items(value))


def _counted(counter: dict[str, int], limit: int | None = None, key: str = "label") -> list[dict]:
	rows = sorted(counter.items(), key=lambda x: (-x[1], x[0]))
	if limit:
		rows = rows[:limit]
	return [{key: k, "count": v} for k, v in rows]


def _bump(counter: dict, key, n: int = 1):
	counter[key] = counter.get(key, 0) + n


def _week_start(day: datetime.date) -> datetime.date:
	"""The Sunday that opens a school week here."""
	return add_days(day, -((day.weekday() + 1) % 7))


def _bucket_key(day: datetime.date, bucket: str) -> str:
	if bucket == "month":
		return f"{day.year}-{day.month:02d}"
	if bucket == "week":
		return str(_week_start(day))
	return str(day)


def _bucket_label(key: str, bucket: str) -> str:
	if bucket == "month":
		y, m = key.split("-")
		return f"{MONTH_AR[int(m) - 1]} {y}"
	if bucket == "week":
		return f"أسبوع {key[5:]}"
	return key[5:]


def _buckets(start, end, bucket: str) -> list[str]:
	keys, seen = [], set()
	day = getdate(start)
	while day <= getdate(end):
		k = _bucket_key(day, bucket)
		if k not in seen:
			seen.add(k)
			keys.append(k)
		day = add_days(day, 1)
	return keys


def _visits(start, end, program=None, student_group=None) -> tuple[list, dict]:
	place = _placement()
	rows = frappe.db.sql(
		"""select name, student, student_name, visit_date, visit_type, complaint, treatment,
			outcome, parent_notified, attended_by
		from `tabMS Health Visit`
		where visit_date >= %(d1)s and visit_date < %(d2x)s
		order by visit_date desc""",
		{"d1": str(start), "d2x": str(add_days(end, 1))},
		as_dict=True,
	)
	return [r for r in rows if _in_scope(place.get(r.student), program, student_group)], place


def _visit_row(r, place: dict) -> dict:
	p = place.get(r.student) or {}
	stamp = str(r.visit_date or "")
	return {
		"id": r.name,
		"student": r.student,
		"studentName": r.student_name or r.student,
		"section": p.get("sectionLabel") or "—",
		"program": p.get("program") or "",
		"date": stamp[:10],
		# A visit typed in without a time is stored at midnight; showing
		# "00:00" would claim a time nobody recorded.
		"time": stamp[11:16] if stamp[11:16] not in ("", "00:00") else "",
		"type": VISIT_TYPE_AR.get(r.visit_type, r.visit_type or "—"),
		"complaint": r.complaint or "",
		"treatment": r.treatment or "",
		"outcome": OUTCOME_AR.get(r.outcome, r.outcome or "—"),
		"outcomeKey": r.outcome or "",
		"parentNotified": bool(cint(r.parent_notified)),
	}


# --- Endpoints --------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*NURSING)
def options(persona: str = None):
	"""Grades and sections to filter by, and the range the screen opens on."""
	start, end = _range()
	filters: dict = {"disabled": 0}
	apply_period(filters, "Student Group")
	groups = frappe.get_all(
		"Student Group",
		filters=filters,
		fields=["name", "student_group_name", "program"],
		order_by="program asc, student_group_name asc",
		limit_page_length=0,
	)
	return {
		"dateFrom": str(start),
		"dateTo": str(end),
		"programs": [{"id": p, "label": p} for p in sorted({g.program for g in groups if g.program})],
		"sections": [{"id": g.name, "label": g.student_group_name or g.name, "program": g.program or ""} for g in groups],
		"visitTypes": [{"id": k, "label": v} for k, v in VISIT_TYPE_AR.items()],
		"outcomes": [{"id": k, "label": v} for k, v in OUTCOME_AR.items()],
	}


@frappe.whitelist()
@ms_endpoint(*NURSING)
def visits_report(
	date_from: str = None,
	date_to: str = None,
	program: str = None,
	student_group: str = None,
	bucket: str = None,
	persona: str = None,
):
	"""Clinic visits over a period: how many, when, why, from where, and what came of them."""
	start, end = _range(date_from, date_to)
	if bucket not in ("day", "week", "month"):
		bucket = "day" if (end - start).days < MAX_DAILY_POINTS else ("week" if (end - start).days <= 200 else "month")
	if bucket == "day" and (end - start).days >= MAX_DAILY_POINTS:
		bucket = "week"

	rows, place = _visits(start, end, program, student_group)
	chronic = {
		r.student
		for r in frappe.get_all(
			"MS Health Record",
			filters={"student": ["in", list({v.student for v in rows}) or [""]]},
			fields=["student", "chronic_conditions", "allergies", "medications"],
			limit_page_length=0,
		)
		if _has(r.chronic_conditions) or _has(r.allergies) or _has(r.medications)
	}

	series = {k: {"visits": 0, "sentHome": 0} for k in _buckets(start, end, bucket)}
	by_type: dict[str, int] = {}
	by_outcome: dict[str, int] = {}
	by_complaint: dict[str, int] = {}
	by_program: dict[str, int] = {}
	by_section: dict[str, int] = {}
	by_weekday: dict[str, int] = {}
	by_hour: dict[str, int] = {}
	by_attendant: dict[str, int] = {}
	per_student: dict[str, dict] = {}
	notified = 0

	for r in rows:
		day = getdate(r.visit_date)
		key = _bucket_key(day, bucket)
		if key in series:
			series[key]["visits"] += 1
			if r.outcome == "Sent Home":
				series[key]["sentHome"] += 1
		_bump(by_type, VISIT_TYPE_AR.get(r.visit_type, r.visit_type or "غير محدد"))
		_bump(by_outcome, OUTCOME_AR.get(r.outcome, r.outcome or "غير محدد"))
		for c in _items(r.complaint) or ["غير مذكورة"]:
			_bump(by_complaint, c)
		p = place.get(r.student) or {}
		_bump(by_program, p.get("program") or "—")
		_bump(by_section, p.get("sectionLabel") or "—")
		_bump(by_weekday, WEEK[day.weekday()])
		hhmm = str(r.visit_date)[11:13]
		if hhmm and str(r.visit_date)[11:16] != "00:00":
			_bump(by_hour, f"{hhmm}:00")
		if r.attended_by:
			_bump(by_attendant, r.attended_by)
		notified += 1 if cint(r.parent_notified) else 0

		s = per_student.setdefault(
			r.student,
			{
				"student": r.student,
				"studentName": r.student_name or r.student,
				"section": p.get("sectionLabel") or "—",
				"program": p.get("program") or "",
				"visits": 0,
				"sentHome": 0,
				"lastVisit": str(r.visit_date)[:10],
				"complaints": {},
				"chronic": r.student in chronic,
			},
		)
		s["visits"] += 1
		if r.outcome == "Sent Home":
			s["sentHome"] += 1
		for c in _items(r.complaint):
			_bump(s["complaints"], c)

	frequent = sorted(per_student.values(), key=lambda s: (-s["visits"], s["studentName"]))[:20]
	for s in frequent:
		s["topComplaint"] = max(s["complaints"], key=s["complaints"].get) if s["complaints"] else ""
		del s["complaints"]

	names = {}
	if by_attendant:
		names = {
			u.name: u.full_name or u.name
			for u in frappe.get_all("User", filters={"name": ["in", list(by_attendant)]}, fields=["name", "full_name"])
		}

	now = getdate(today())
	week_start = _week_start(now)
	total = len(rows)
	school_days = sum(
		1 for i in range((end - start).days + 1) if WEEK[add_days(start, i).weekday()] not in ("Friday", "Saturday")
	)
	return {
		"dateFrom": str(start),
		"dateTo": str(end),
		"bucket": bucket,
		"summary": {
			"visits": total,
			"students": len(per_student),
			"repeatVisitors": sum(1 for s in per_student.values() if s["visits"] >= 3),
			"sentHome": sum(1 for r in rows if r.outcome == "Sent Home"),
			"referred": sum(1 for r in rows if r.outcome == "Referred to Hospital"),
			"returned": sum(1 for r in rows if r.outcome == "Returned to Class"),
			"rested": sum(1 for r in rows if r.outcome == "Rest in Clinic"),
			"injuries": sum(1 for r in rows if r.visit_type == "Injury"),
			"parentNotified": notified,
			"parentNotifiedPct": round(notified / total * 100, 1) if total else None,
			"avgPerDay": round(total / school_days, 1) if school_days else 0,
			"today": sum(1 for r in rows if getdate(r.visit_date) == now),
			"thisWeek": sum(1 for r in rows if week_start <= getdate(r.visit_date) <= now),
		},
		"series": [{"key": k, "label": _bucket_label(k, bucket), **v} for k, v in series.items()],
		"byType": _counted(by_type),
		"byOutcome": _counted(by_outcome),
		"byComplaint": _counted(by_complaint, 15),
		"byProgram": _counted(by_program),
		"bySection": _counted(by_section),
		"byWeekday": [
			{"label": DAY_AR[d], "count": by_weekday.get(d, 0)}
			for d in ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
			if by_weekday.get(d) or d not in ("Friday", "Saturday")
		],
		"byHour": [{"label": k, "count": by_hour[k]} for k in sorted(by_hour)],
		"byAttendant": [{"label": names.get(k, k), "count": v} for k, v in sorted(by_attendant.items(), key=lambda x: -x[1])],
		"frequent": frequent,
		"recent": [_visit_row(r, place) for r in rows[:15]],
	}


@frappe.whitelist()
@ms_endpoint(*NURSING)
def records_report(program: str = None, student_group: str = None, persona: str = None):
	"""The health register as a whole: who needs watching, and what the nurse
	does not know yet (a pupil with no record, a check-up long overdue)."""
	place = _placement()
	students = {
		s.name: s
		for s in frappe.get_all("Student", filters={"enabled": 1}, fields=["name", "student_name"], limit_page_length=0)
	}
	in_scope = {k for k in students if _in_scope(place.get(k), program, student_group)}
	records = frappe.get_all(
		"MS Health Record",
		filters={"student": ["in", list(in_scope) or [""]]},
		fields=[
			"student", "student_name", "blood_group", "height_cm", "weight_kg", "chronic_conditions",
			"allergies", "medications", "special_needs", "last_checkup", "emergency_contact_phone",
		],
		order_by="modified desc",
		limit_page_length=0,
	)
	seen: set[str] = set()
	unique = []
	for r in records:
		if r.student not in seen:
			seen.add(r.student)
			unique.append(r)

	year_ago = add_days(getdate(today()), -365)
	conditions: dict[str, int] = {}
	allergies: dict[str, int] = {}
	medications: dict[str, int] = {}
	blood: dict[str, int] = {}
	by_program: dict[str, dict] = {}
	cases = []
	counts = {"chronic": 0, "allergies": 0, "medications": 0, "specialNeeds": 0, "overdue": 0, "noEmergency": 0, "noBlood": 0}

	for r in unique:
		p = place.get(r.student) or {}
		cond, alg, med, needs = _items(r.chronic_conditions), _items(r.allergies), _items(r.medications), _items(r.special_needs)
		for c in cond:
			_bump(conditions, c)
		for a in alg:
			_bump(allergies, a)
		for m in med:
			_bump(medications, m)
		_bump(blood, r.blood_group or "غير مسجّل")
		counts["chronic"] += 1 if cond else 0
		counts["allergies"] += 1 if alg else 0
		counts["medications"] += 1 if med else 0
		counts["specialNeeds"] += 1 if needs else 0
		counts["overdue"] += 1 if (not r.last_checkup or getdate(r.last_checkup) < year_ago) else 0
		counts["noEmergency"] += 0 if (r.emergency_contact_phone or "").strip() else 1
		counts["noBlood"] += 0 if r.blood_group else 1

		g = by_program.setdefault(
			p.get("program") or "—", {"label": p.get("program") or "—", "chronic": 0, "allergies": 0, "medications": 0}
		)
		g["chronic"] += 1 if cond else 0
		g["allergies"] += 1 if alg else 0
		g["medications"] += 1 if med else 0

		if cond or alg or med or needs:
			cases.append(
				{
					"student": r.student,
					"studentName": r.student_name or students.get(r.student, {}).get("student_name") or r.student,
					"section": p.get("sectionLabel") or "—",
					"program": p.get("program") or "",
					"bloodGroup": r.blood_group or "",
					"conditions": "، ".join(cond),
					"allergies": "، ".join(alg),
					"medications": "، ".join(med),
					"specialNeeds": "، ".join(needs),
					"lastCheckup": str(r.last_checkup) if r.last_checkup else None,
				}
			)

	cases.sort(key=lambda c: (c["program"], c["section"], c["studentName"]))
	return {
		"summary": {
			"students": len(in_scope),
			"withRecord": len(unique),
			"withoutRecord": len(in_scope) - len(unique),
			"coveragePct": round(len(unique) / len(in_scope) * 100, 1) if in_scope else None,
			**counts,
		},
		"conditions": _counted(conditions, 15),
		"allergies": _counted(allergies, 15),
		"medications": _counted(medications, 15),
		"bloodGroups": _counted(blood),
		"byProgram": sorted(by_program.values(), key=lambda g: g["label"]),
		"cases": cases[:500],
	}


@frappe.whitelist()
@ms_endpoint(*NURSING)
def dashboard(persona: str = None):
	"""The nurse's opening view: today, this week, the last fortnight, the latest visits."""
	now = getdate(today())
	start = add_days(now, -29)
	rows, place = _visits(start, now)
	week_start = _week_start(now)
	daily: dict[str, int] = {}
	for r in rows:
		_bump(daily, str(getdate(r.visit_date)))

	todays = [r for r in rows if getdate(r.visit_date) == now]
	week = [r for r in rows if week_start <= getdate(r.visit_date) <= now]

	chronic = allergic = medicated = 0
	for r in frappe.db.sql(
		"""select h.chronic_conditions, h.allergies, h.medications
		from `tabMS Health Record` h join `tabStudent` s on s.name = h.student and s.enabled = 1""",
		as_dict=True,
	):
		chronic += 1 if _has(r.chronic_conditions) else 0
		allergic += 1 if _has(r.allergies) else 0
		medicated += 1 if _has(r.medications) else 0

	last14 = [add_days(now, -i) for i in range(13, -1, -1)]
	return {
		"today": len(todays),
		"todaySentHome": sum(1 for r in todays if r.outcome == "Sent Home"),
		"thisWeek": len(week),
		"last30": len(rows),
		"chronic": chronic,
		"allergies": allergic,
		"medications": medicated,
		"series": [
			{"date": str(d), "label": str(d)[5:], "weekday": DAY_AR[WEEK[d.weekday()]], "visits": daily.get(str(d), 0)}
			for d in last14
		],
		# The latest visits whenever they were: a quiet month should still show
		# the nurse where the log left off, not an empty list.
		"recent": [
			_visit_row(r, place)
			for r in frappe.db.sql(
				"""select name, student, student_name, visit_date, visit_type, complaint, treatment,
					outcome, parent_notified, attended_by
				from `tabMS Health Visit` order by visit_date desc, creation desc limit 8""",
				as_dict=True,
			)
		],
	}


@frappe.whitelist()
@ms_endpoint(*NURSING)
def student_visits(student: str = None, persona: str = None):
	"""One frequent visitor's history, opened from the report."""
	if not student or not frappe.db.exists("Student", student):
		return fail("Student not found.", "الطالب غير موجود.")
	place = _placement()
	rows = frappe.db.sql(
		"""select name, student, student_name, visit_date, visit_type, complaint, treatment,
			outcome, parent_notified, attended_by
		from `tabMS Health Visit` where student = %(s)s order by visit_date desc limit 100""",
		{"s": student},
		as_dict=True,
	)
	return {"student": student, "visits": [_visit_row(r, place) for r in rows]}
