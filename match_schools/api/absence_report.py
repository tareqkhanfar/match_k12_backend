"""تقرير الغياب — who was absent, when, how often, and whom to call.

One record per pupil per school day (`Student Attendance`): حاضر، غائب، or
excused (`Excused` / `Leave`, with `ms_absence_reason`). An excused day is not
counted against the pupil — the rate is absences over present + absent, the
same rule attendance certificates use.

Scope follows the role, the same way the register does:
* the office and the vice principal: the whole school;
* a teacher: the sections they teach or are مربي of — and a supervisor, whose
  "sections" are the ones they supervise (utils.SUPERVISOR_SCOPE);
* a parent or a pupil: their own children only, without anyone's contacts.
"""

import datetime

import frappe
from frappe.utils import add_days, getdate, nowdate

from match_schools.api.utils import (
	BACK_OFFICE,
	ROLE_PARENT,
	ROLE_STUDENT,
	ROLE_TEACHER,
	apply_period,
	fail,
	get_default_academic_term,
	instructor_groups,
	ms_endpoint,
	resolve_scope,
)

ALL = (*BACK_OFFICE, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
EXCUSED = ("Excused", "Leave")
STATUS_AR = {"Present": "حاضر", "Absent": "غائب", "Excused": "بعذر", "Leave": "إجازة"}
WEEKDAY_AR = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
RELATION_AR = {"Father": "الأب", "Mother": "الأم", "Others": "آخر", "Other": "آخر", "Guardian": "ولي أمر"}
MAX_DAYS = 400
MAX_ROWS = 5000
STREAK = 3
CHRONIC_PCT = 10.0


# --- scope -------------------------------------------------------------------------


def _scope(persona: str) -> dict:
	"""What this caller may read: {"groups": [...] | None, "students": [...] | None}."""
	if persona in BACK_OFFICE:
		return {"groups": None, "students": None}
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		instructor = scope.get("instructor")
		groups = set(instructor_groups(instructor, period=False))
		if instructor:
			groups |= set(
				frappe.get_all(
					"Student Group",
					filters={"ms_homeroom_instructor": instructor, "disabled": 0},
					pluck="name",
				)
			)
		return {"groups": sorted(groups), "students": None}
	return {"groups": None, "students": list(scope.get("students") or [])}


def _family(persona: str) -> bool:
	return persona in (ROLE_STUDENT, ROLE_PARENT)


def _range(date_from: str | None, date_to: str | None) -> tuple[datetime.date, datetime.date]:
	end = getdate(date_to or nowdate())
	if date_from:
		start = getdate(date_from)
	else:
		# The term so far, or the last 30 days without a term.
		term = get_default_academic_term()
		term_start = frappe.db.get_value("Academic Term", term, "term_start_date") if term else None
		start = getdate(term_start) if term_start else add_days(end, -29)
		start = getdate(start)
	if start > end:
		start, end = end, start
	if (end - start).days > MAX_DAYS:
		start = end - datetime.timedelta(days=MAX_DAYS)
	return start, end


def _where(persona: str, start, end, program=None, student_group=None, student=None) -> tuple[str, dict] | None:
	"""The WHERE clause for this caller and these filters; None when they may see nothing."""
	scope = _scope(persona)
	cond = ["sa.docstatus < 2", "sa.date BETWEEN %(start)s AND %(end)s"]
	params: dict = {"start": str(start), "end": str(end)}
	if scope["groups"] is not None:
		if not scope["groups"]:
			return None
		cond.append("sa.student_group IN %(scope_groups)s")
		params["scope_groups"] = scope["groups"]
	if scope["students"] is not None:
		if not scope["students"]:
			return None
		cond.append("sa.student IN %(scope_students)s")
		params["scope_students"] = scope["students"]
	if student_group:
		if scope["groups"] is not None and student_group not in scope["groups"]:
			frappe.throw("هذه الشعبة ليست من شعبك.", frappe.PermissionError)
		cond.append("sa.student_group = %(group)s")
		params["group"] = student_group
	elif program:
		groups = frappe.get_all("Student Group", filters={"program": program}, pluck="name", limit_page_length=0)
		cond.append("sa.student_group IN %(program_groups)s")
		params["program_groups"] = groups or [""]
	if student:
		if scope["students"] is not None and student not in scope["students"]:
			frappe.throw("هذا الطالب ليس من أبنائك.", frappe.PermissionError)
		cond.append("sa.student = %(student)s")
		params["student"] = student
	return " AND ".join(cond), params


def _rate(absent: int, present: int) -> float | None:
	total = absent + present
	return round(100.0 * absent / total, 1) if total else None


# --- options -----------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*ALL)
def options(persona: str = None):
	"""The grades, sections and pupils this caller can filter by, and the range
	the report opens on."""
	start, end = _range(None, None)
	scope = _scope(persona)
	f: dict = {"disabled": 0}
	apply_period(f, "Student Group")
	if scope["groups"] is not None:
		f["name"] = ["in", scope["groups"] or [""]]
	groups = frappe.get_all(
		"Student Group",
		filters=f,
		fields=["name", "student_group_name", "program"],
		order_by="program asc, student_group_name asc",
		limit_page_length=0,
	)
	if scope["students"] is not None:
		students = frappe.get_all(
			"Student",
			filters={"name": ["in", scope["students"] or [""]]},
			fields=["name", "student_name"],
		)
		groups = []
	else:
		rows = frappe.db.sql(
			"""SELECT DISTINCT sgs.student, COALESCE(s.student_name, sgs.student) AS student_name, sgs.parent
			     FROM `tabStudent Group Student` sgs JOIN `tabStudent` s ON s.name = sgs.student
			    WHERE sgs.parenttype = 'Student Group' AND sgs.active = 1 AND sgs.parent IN %(g)s
			    ORDER BY student_name""",
			{"g": [g.name for g in groups] or [""]},
			as_dict=True,
		)
		seen, students = set(), []
		for r in rows:
			if r.student not in seen:
				seen.add(r.student)
				students.append(frappe._dict(name=r.student, student_name=r.student_name))
	programs = sorted({g.program for g in groups if g.program})
	reasons = [
		r[0]
		for r in frappe.db.sql(
			"""SELECT ms_absence_reason, COUNT(*) n FROM `tabStudent Attendance`
			    WHERE IFNULL(ms_absence_reason, '') != '' AND docstatus < 2
			    GROUP BY ms_absence_reason ORDER BY n DESC LIMIT 30"""
		)
	]
	return {
		"defaultRange": {"dateFrom": str(start), "dateTo": str(end)},
		"programs": [{"id": p, "label": p} for p in programs],
		"sections": [
			{"id": g.name, "label": g.student_group_name or g.name, "program": g.program or ""} for g in groups
		],
		"students": [{"id": s.name, "label": s.student_name or s.name} for s in students],
		"reasons": reasons,
		"family": _family(persona),
		"showsContacts": not _family(persona),
		"scope": "school" if persona in BACK_OFFICE else ("family" if _family(persona) else "sections"),
	}


# --- the report --------------------------------------------------------------------


def _guardians(students: list[str]) -> dict[str, list[dict]]:
	"""Each pupil's guardians with their numbers, first listed first."""
	if not students:
		return {}
	rows = frappe.db.sql(
		"""SELECT sg.parent AS student, sg.guardian, sg.guardian_name, sg.relation,
		          g.mobile_number, g.alternate_number, g.email_address
		     FROM `tabStudent Guardian` sg
		     LEFT JOIN `tabGuardian` g ON g.name = sg.guardian
		    WHERE sg.parenttype = 'Student' AND sg.parent IN %(s)s
		    ORDER BY sg.parent, sg.idx""",
		{"s": students},
		as_dict=True,
	)
	out: dict[str, list[dict]] = {}
	for r in rows:
		out.setdefault(r.student, []).append(
			{
				"id": r.guardian or "",
				"name": r.guardian_name or "",
				"relation": RELATION_AR.get(r.relation or "", r.relation or ""),
				"mobile": r.mobile_number or "",
				"altMobile": r.alternate_number or "",
				"email": r.email_address or "",
			}
		)
	return out


def _streaks(records: list[tuple[str, str]]) -> tuple[int, int, str]:
	"""(longest run, current run, start of current run) of absences over a
	pupil's recorded days, oldest first. Days with no register are skipped,
	so a weekend does not break a run."""
	best = cur = 0
	cur_start = ""
	for day, status in records:
		if status == "Absent":
			if cur == 0:
				cur_start = day
			cur += 1
			best = max(best, cur)
		elif status == "Present":
			cur = 0
			cur_start = ""
		# An excused day neither extends nor breaks a run.
	return best, cur, cur_start if cur else ""


@frappe.whitelist()
@ms_endpoint(*ALL)
def report(
	date_from: str = None,
	date_to: str = None,
	program: str = None,
	student_group: str = None,
	student: str = None,
	persona: str = None,
):
	"""Everything the absence report shows for one range and one filter."""
	start, end = _range(date_from, date_to)
	built = _where(persona, start, end, program, student_group, student)
	period = {"dateFrom": str(start), "dateTo": str(end), "singleDay": start == end}
	if not built:
		return {"period": period, "empty": True}
	where, params = built

	rows = frappe.db.sql(
		f"""SELECT sa.name, sa.student, sa.student_name, sa.student_group, sa.date, sa.status,
		           sa.ms_absence_reason AS reason
		      FROM `tabStudent Attendance` sa
		     WHERE {where}
		     ORDER BY sa.date ASC
		     LIMIT {MAX_ROWS * 10}""",
		params,
		as_dict=True,
	)
	groups = {r.student_group for r in rows if r.student_group}
	group_info = {
		g.name: g
		for g in frappe.get_all(
			"Student Group",
			filters={"name": ["in", list(groups) or [""]]},
			fields=["name", "student_group_name", "program"],
		)
	}

	def label(g):
		gi = group_info.get(g)
		return (gi.student_group_name if gi else "") or g or ""

	def program_of(g):
		gi = group_info.get(g)
		return (gi.program if gi else "") or ""

	present = sum(1 for r in rows if r.status == "Present")
	absent = sum(1 for r in rows if r.status == "Absent")
	excused = sum(1 for r in rows if r.status in EXCUSED)

	# Per day and weekday.
	days: dict[str, dict] = {}
	for r in rows:
		d = days.setdefault(str(r.date), {"present": 0, "absent": 0, "excused": 0})
		key = "present" if r.status == "Present" else "absent" if r.status == "Absent" else "excused"
		d[key] += 1
	daily = [
		{
			"date": k,
			"weekday": WEEKDAY_AR[getdate(k).weekday()],
			**v,
			"rate": _rate(v["absent"], v["present"]),
		}
		for k, v in sorted(days.items())
	]
	weekday: dict[str, dict] = {}
	for d in daily:
		w = weekday.setdefault(d["weekday"], {"day": d["weekday"], "absent": 0, "present": 0, "excused": 0, "days": 0})
		w["absent"] += d["absent"]
		w["present"] += d["present"]
		w["excused"] += d["excused"]
		w["days"] += 1
	order = ["الأحد", "الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]
	by_weekday = [
		{**w, "rate": _rate(w["absent"], w["present"]), "avgAbsent": round(w["absent"] / w["days"], 1) if w["days"] else 0}
		for w in sorted(weekday.values(), key=lambda w: order.index(w["day"]) if w["day"] in order else 9)
	]

	# Per pupil.
	per: dict[str, dict] = {}
	history: dict[str, list[tuple[str, str]]] = {}
	for r in rows:
		p = per.setdefault(
			r.student,
			{
				"student": r.student,
				"name": r.student_name or r.student,
				"section": r.student_group or "",
				"present": 0,
				"absent": 0,
				"excused": 0,
				"lastAbsent": "",
			},
		)
		p["section"] = r.student_group or p["section"]
		if r.status == "Present":
			p["present"] += 1
		elif r.status == "Absent":
			p["absent"] += 1
			p["lastAbsent"] = str(r.date)
		else:
			p["excused"] += 1
		history.setdefault(r.student, []).append((str(r.date), r.status))
	students = []
	for sid, p in per.items():
		best, cur, cur_start = _streaks(history.get(sid, []))
		students.append(
			{
				**p,
				"sectionLabel": label(p["section"]),
				"program": program_of(p["section"]),
				"days": p["present"] + p["absent"] + p["excused"],
				"rate": _rate(p["absent"], p["present"]),
				"longestStreak": best,
				"currentStreak": cur,
				"currentStreakFrom": cur_start,
			}
		)
	students.sort(key=lambda s: (-s["absent"], -(s["rate"] or 0), s["name"]))

	# Per section and grade.
	def group_rows(key_of, label_of):
		acc: dict[str, dict] = {}
		for r in rows:
			k = key_of(r.student_group)
			a = acc.setdefault(k, {"id": k, "label": label_of(r.student_group), "present": 0, "absent": 0, "excused": 0, "students": set(), "absentStudents": set()})
			if r.status == "Present":
				a["present"] += 1
			elif r.status == "Absent":
				a["absent"] += 1
				a["absentStudents"].add(r.student)
			else:
				a["excused"] += 1
			a["students"].add(r.student)
		out = []
		for a in acc.values():
			out.append(
				{
					"id": a["id"],
					"label": a["label"],
					"present": a["present"],
					"absent": a["absent"],
					"excused": a["excused"],
					"students": len(a["students"]),
					"absentStudents": len(a["absentStudents"]),
					"rate": _rate(a["absent"], a["present"]),
				}
			)
		out.sort(key=lambda x: -(x["rate"] or 0))
		return out

	by_section = group_rows(lambda g: g or "", label)
	by_grade = group_rows(lambda g: program_of(g) or "—", lambda g: program_of(g) or "—")

	# Reasons, for every non-present day that has one.
	reasons: dict[str, int] = {}
	for r in rows:
		if r.status != "Present":
			key = (r.reason or "").strip() or ("بلا سبب مسجّل" if r.status == "Absent" else "بعذر — بلا سبب مكتوب")
			reasons[key] = reasons.get(key, 0) + 1
	by_reason = sorted(({"label": k, "count": v} for k, v in reasons.items()), key=lambda x: -x["count"])

	# The absentees, newest first, with whom to call.
	missing = [r for r in reversed(rows) if r.status != "Present"][:MAX_ROWS]
	contacts = {} if _family(persona) else _guardians(sorted({r.student for r in missing}))
	absentees = [
		{
			"id": r.name,
			"date": str(r.date),
			"weekday": WEEKDAY_AR[getdate(r.date).weekday()],
			"student": r.student,
			"name": r.student_name or r.student,
			"section": r.student_group or "",
			"sectionLabel": label(r.student_group),
			"status": r.status,
			"statusLabel": STATUS_AR.get(r.status, r.status),
			"reason": r.reason or "",
			"guardians": contacts.get(r.student, []),
		}
		for r in missing
	]

	worst = max(daily, key=lambda d: d["absent"], default=None)
	streaks = [s for s in students if s["longestStreak"] >= STREAK]
	chronic = [s for s in students if (s["rate"] or 0) >= CHRONIC_PCT and s["absent"] >= 2]
	return {
		"period": period,
		"summary": {
			"records": len(rows),
			"present": present,
			"absent": absent,
			"excused": excused,
			"rate": _rate(absent, present),
			"students": len(per),
			"absentStudents": sum(1 for s in students if s["absent"]),
			"perfect": sum(1 for s in students if not s["absent"] and not s["excused"]),
			"schoolDays": len(daily),
			"avgDailyAbsent": round(absent / len(daily), 1) if daily else 0,
			"worstDay": {"date": worst["date"], "weekday": worst["weekday"], "absent": worst["absent"]} if worst and worst["absent"] else None,
			"streaks": len(streaks),
			"currentStreaks": sum(1 for s in students if s["currentStreak"] >= STREAK),
			"chronic": len(chronic),
			"chronicPct": CHRONIC_PCT,
			"streakDays": STREAK,
		},
		"daily": daily,
		"weekday": by_weekday,
		"students": students,
		"sections": by_section,
		"grades": by_grade,
		"reasons": by_reason,
		"absentees": absentees,
		"truncated": len(missing) >= MAX_ROWS,
		"showsContacts": not _family(persona),
	}


@frappe.whitelist()
@ms_endpoint(*ALL)
def student_file(student: str, date_from: str = None, date_to: str = None, persona: str = None):
	"""One pupil's absences in the range: the days, the reasons, the runs, the
	month by month, and — for staff — the guardians to call."""
	if not student or not frappe.db.exists("Student", student):
		return fail("Student not found.", "لم يتم العثور على الطالب.")
	start, end = _range(date_from, date_to)
	scope = _scope(persona)
	if scope["students"] is not None and student not in scope["students"]:
		frappe.throw("هذا الطالب ليس من أبنائك.", frappe.PermissionError)
	built = _where(persona, start, end, student=student)
	if not built:
		frappe.throw("هذا الطالب خارج شعبك.", frappe.PermissionError)
	where, params = built
	rows = frappe.db.sql(
		f"""SELECT sa.date, sa.status, sa.ms_absence_reason AS reason, sa.student_group
		      FROM `tabStudent Attendance` sa WHERE {where} ORDER BY sa.date ASC""",
		params,
		as_dict=True,
	)
	if scope["groups"] is not None and not rows:
		in_mine = frappe.db.exists(
			"Student Group Student",
			{"student": student, "parent": ["in", scope["groups"] or [""]], "active": 1},
		)
		if not in_mine:
			frappe.throw("هذا الطالب خارج شعبك.", frappe.PermissionError)
	present = sum(1 for r in rows if r.status == "Present")
	absent = sum(1 for r in rows if r.status == "Absent")
	excused = sum(1 for r in rows if r.status in EXCUSED)
	best, cur, cur_start = _streaks([(str(r.date), r.status) for r in rows])
	months: dict[str, dict] = {}
	for r in rows:
		m = months.setdefault(str(r.date)[:7], {"month": str(r.date)[:7], "present": 0, "absent": 0, "excused": 0})
		key = "present" if r.status == "Present" else "absent" if r.status == "Absent" else "excused"
		m[key] += 1
	st = frappe.db.get_value("Student", student, ["student_name", "image"], as_dict=True) or {}
	group = rows[-1].student_group if rows else None
	return {
		"student": student,
		"name": st.get("student_name") or student,
		"image": st.get("image"),
		"sectionLabel": (frappe.db.get_value("Student Group", group, "student_group_name") or group) if group else "",
		"period": {"dateFrom": str(start), "dateTo": str(end)},
		"summary": {
			"days": len(rows),
			"present": present,
			"absent": absent,
			"excused": excused,
			"rate": _rate(absent, present),
			"longestStreak": best,
			"currentStreak": cur,
			"currentStreakFrom": cur_start,
		},
		"months": [{**m, "rate": _rate(m["absent"], m["present"])} for m in sorted(months.values(), key=lambda m: m["month"])],
		"days": [
			{
				"date": str(r.date),
				"weekday": WEEKDAY_AR[getdate(r.date).weekday()],
				"status": r.status,
				"statusLabel": STATUS_AR.get(r.status, r.status),
				"reason": r.reason or "",
			}
			for r in reversed(rows)
			if r.status != "Present"
		],
		"guardians": [] if _family(persona) else _guardians([student]).get(student, []),
	}
