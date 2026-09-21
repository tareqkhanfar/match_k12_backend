"""أوقات الدوام — شكل اليوم الدراسي لكل صف.

مدرسة واحدة لا تعني جرساً واحداً: الصفوف الدنيا تستريح قبل الحصة الرابعة
والعليا بعدها، وقد تدرس مرحلة خمس حصص بينما تدرس أخرى ثماني. هنا يُعرَّف كل
توقيت مرة واحدة — عدد الحصص ومدّتها ومكان الاستراحة — ثم يُسنَد إلى الصفوف
والشُعب، فتقرأ منه كل شاشة تعرض جدولاً أو تبنيه.

الأوقات المحفوظة في الحصص تبقى هي المصدر لما هو مبنيّ فعلاً؛ التوقيت هنا هو
ما تُبنى عليه الجداول الجديدة، و«تطبيق الأوقات» هو ما يجعل القديم يتبعه.
"""

import frappe
from frappe.utils import cint

from match_schools.api import scheduling as sched
from match_schools.api.utils import (
	fail,
	ms_endpoint,
	parse_json_arg,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
)


# --- Reading ------------------------------------------------------------------


def _rows(schedule: str) -> list[dict]:
	"""One schedule's day, breaks included, in the order it runs."""
	rows = frappe.get_all(
		"MS Timetable Period",
		filters={"parent": schedule, "parenttype": "MS Bell Schedule"},
		fields=["period_name", "period_order", "from_time", "to_time", "is_break"],
		order_by="from_time asc",
		limit_page_length=0,
	)
	return [
		{
			"name": r.period_name,
			"order": cint(r.period_order),
			"from": sched.hhmmss(r.from_time)[:5],
			"to": sched.hhmmss(r.to_time)[:5],
			"isBreak": bool(cint(r.is_break)),
		}
		for r in rows
	]


@frappe.request_cache
def _all_rows() -> dict:
	"""Every schedule's day in one query — the grids ask for all of them."""
	out: dict[str, list[dict]] = {}
	for r in frappe.get_all(
		"MS Timetable Period",
		filters={"parenttype": "MS Bell Schedule"},
		fields=["parent", "period_name", "period_order", "from_time", "to_time", "is_break"],
		order_by="parent asc, from_time asc",
		limit_page_length=0,
	):
		out.setdefault(r.parent, []).append({
			"name": r.period_name,
			"order": cint(r.period_order),
			"from": sched.hhmmss(r.from_time)[:5],
			"to": sched.hhmmss(r.to_time)[:5],
			"isBreak": bool(cint(r.is_break)),
		})
	return out


@frappe.request_cache
def _assignments() -> tuple[dict, dict, str | None]:
	"""Who follows which schedule: sections, grades, and the fallback."""
	if not frappe.db.has_column("Student Group", "ms_bell_schedule"):
		return {}, {}, None

	sections = {
		r.name: r.ms_bell_schedule
		for r in frappe.get_all(
			"Student Group",
			filters={"ms_bell_schedule": ["is", "set"]},
			fields=["name", "ms_bell_schedule"],
			limit_page_length=0,
		)
	}
	programs = {
		r.name: r.ms_bell_schedule
		for r in frappe.get_all(
			"Program",
			filters={"ms_bell_schedule": ["is", "set"]},
			fields=["name", "ms_bell_schedule"],
			limit_page_length=0,
		)
	}
	fallback = frappe.db.get_value("MS Bell Schedule", {"is_default": 1}, "name")
	return sections, programs, fallback


def schedule_of(student_group: str | None, use_default: bool = True) -> str | None:
	"""The schedule one section follows: its own, its grade's, or the default.

	Only the first two are an *assignment* — somebody decided this section runs
	that day. The default is a stand-in for sections nobody has decided about,
	so callers that would rewrite or override something built pass
	`use_default=False`: a default set casually must never be able to re-time
	a week that was built on a different bell.
	"""
	if not student_group:
		return None
	sections, programs, fallback = _assignments()
	if student_group in sections:
		return sections[student_group]
	program = frappe.db.get_value("Student Group", student_group, "program")
	if program and program in programs:
		return programs[program]
	return fallback if use_default else None


def clock_of(student_group: str | None, use_default: bool = True) -> list[dict]:
	"""The lessons of one section's school day, breaks left out.

	This is configuration, not history: it says what the day is meant to be.
	A section with no schedule assigned and no default returns nothing, and
	the caller falls back to reading the times out of the saved week.
	"""
	schedule = schedule_of(student_group, use_default)
	if not schedule:
		return []
	return [p for p in _all_rows().get(schedule, []) if not p["isBreak"]]


def default_clock() -> list[dict]:
	"""The lessons of the school's default day, for a section with nothing else."""
	_, _, fallback = _assignments()
	if not fallback:
		return []
	return [p for p in _all_rows().get(fallback, []) if not p["isBreak"]]


def day_of(student_group: str | None, use_default: bool = False) -> list[dict]:
	"""The whole day including breaks — what a grid draws as rows."""
	schedule = schedule_of(student_group, use_default)
	return list(_all_rows().get(schedule, [])) if schedule else []


def clocks_of(groups: list[str], use_default: bool = True) -> dict:
	"""Several sections' clocks at once, for a grid that spans the school."""
	if not groups:
		return {}
	sections, programs, fallback = _assignments()
	if not use_default:
		fallback = None
	rows = _all_rows()
	programs_by_group = {}
	for r in frappe.get_all(
		"Student Group",
		filters={"name": ["in", groups]},
		fields=["name", "program"],
		limit_page_length=0,
	):
		programs_by_group[r.name] = r.program

	out = {}
	for group in groups:
		schedule = sections.get(group) or programs.get(programs_by_group.get(group)) or fallback
		out[group] = [p for p in rows.get(schedule, []) if not p["isBreak"]] if schedule else []
	return out


# --- The screen ---------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def list_schedules(persona: str = None):
	"""Every schedule with its day and who follows it."""
	sections, programs, fallback = _assignments()
	rows = _all_rows()
	used_sections: dict[str, list] = {}
	used_programs: dict[str, list] = {}
	for group, schedule in sections.items():
		used_sections.setdefault(schedule, []).append(group)
	for program, schedule in programs.items():
		used_programs.setdefault(schedule, []).append(program)

	schedules = frappe.get_all(
		"MS Bell Schedule",
		fields=["name", "schedule_name", "is_default", "working_days", "notes"],
		order_by="is_default desc, schedule_name asc",
		limit_page_length=0,
	)
	return {
		"schedules": [
			{
				"name": s.name,
				"title": s.schedule_name,
				"isDefault": bool(cint(s.is_default)),
				"workingDays": [d for d in (s.working_days or "").split(",") if d],
				"notes": s.notes,
				"periods": rows.get(s.name, []),
				"lessons": sum(1 for p in rows.get(s.name, []) if not p["isBreak"]),
				"programs": used_programs.get(s.name, []),
				"studentGroups": used_sections.get(s.name, []),
			}
			for s in schedules
		],
		"programs": frappe.get_all(
			"Program",
			fields=["name", "program_name", "ms_bell_schedule as schedule"],
			order_by="name",
			limit_page_length=0,
		)
		if frappe.db.has_column("Program", "ms_bell_schedule")
		else [],
		"studentGroups": frappe.get_all(
			"Student Group",
			filters={"disabled": 0},
			fields=[
				"name",
				"student_group_name",
				"program",
				"ms_bell_schedule as schedule",
			],
			order_by="program, student_group_name",
			limit_page_length=0,
		)
		if frappe.db.has_column("Student Group", "ms_bell_schedule")
		else [],
		"days": [{"value": k, "label": v} for k, v in sched.WEEKDAYS],
		"default": fallback,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_schedule(
	title: str,
	periods: str | list,
	name: str = None,
	is_default: int = 0,
	working_days: str | list = None,
	notes: str = None,
	persona: str = None,
):
	"""Create or replace one schedule's day.

	The rows are numbered here rather than by whoever typed them: a break is
	not a lesson, so the lesson after it is the next number, and the numbers
	are what the timetable stores.
	"""
	rows = parse_json_arg(periods) or []
	if not [r for r in rows if not cint(r.get("isBreak"))]:
		return fail("Add at least one lesson", "أضف حصة واحدة على الأقل.")

	title = (title or "").strip()
	if not title:
		return fail("Name the schedule", "اكتب اسماً للتوقيت.")
	editing = bool(name and frappe.db.exists("MS Bell Schedule", name))
	# The name is what identifies a schedule; a second one with the same name
	# would otherwise surface as a raw duplicate-entry error.
	taken = frappe.db.exists("MS Bell Schedule", {"schedule_name": title})
	if taken and (not editing or taken != name):
		return fail("Name already used", "يوجد توقيت بهذا الاسم — اختر اسماً آخر.")

	doc = frappe.get_doc("MS Bell Schedule", name) if editing else frappe.new_doc("MS Bell Schedule")
	doc.schedule_name = title
	doc.is_default = cint(is_default)
	days = parse_json_arg(working_days)
	doc.working_days = ",".join(days) if isinstance(days, list) else (working_days or None)
	doc.notes = notes
	doc.set("periods", [])
	for row in rows:
		doc.append(
			"periods",
			{
				"period_name": row.get("name") or None,
				"period_order": cint(row.get("order")),
				"from_time": _time(row.get("from")),
				"to_time": _time(row.get("to")),
				"is_break": cint(row.get("isBreak")),
			},
		)
	doc.save(ignore_permissions=True)
	# The id follows the title, so the grades and sections pointing at it (and
	# the labels beside them) read the name the school gave it. Links follow.
	if editing and doc.name != title:
		frappe.rename_doc("MS Bell Schedule", doc.name, title, force=True)
		doc = frappe.get_doc("MS Bell Schedule", title)
	frappe.db.commit()
	return {
		"name": doc.name,
		"periods": _rows(doc.name),
		"message_ar": "تم حفظ التوقيت.",
		"message_en": "Schedule saved.",
	}


def _time(value) -> str | None:
	if value in (None, ""):
		return None
	text = str(value)
	return sched.hhmmss(text if len(text) > 5 else f"{text}:00")


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_schedule(name: str, persona: str = None):
	"""Remove a schedule nothing follows."""
	if not frappe.db.exists("MS Bell Schedule", name):
		return fail("Not found", "التوقيت غير موجود.")
	try:
		frappe.delete_doc("MS Bell Schedule", name, ignore_permissions=True)
	except frappe.ValidationError as e:
		return fail("In use", str(e))
	frappe.db.commit()
	return {"message_ar": "تم حذف التوقيت.", "message_en": "Schedule deleted."}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def assign_schedule(
	name: str = None,
	programs: str | list = None,
	student_groups: str | list = None,
	persona: str = None,
):
	"""Point grades and sections at a schedule — or, with no name, clear them.

	A section assigned directly overrides its grade, which is how a school
	runs one section on a different bell without inventing a grade for it.
	"""
	if name and not frappe.db.exists("MS Bell Schedule", name):
		return fail("Not found", "التوقيت غير موجود.")

	chosen_programs = parse_json_arg(programs) or []
	chosen_groups = parse_json_arg(student_groups) or []
	for program in chosen_programs:
		frappe.db.set_value("Program", program, "ms_bell_schedule", name or None)
	for group in chosen_groups:
		frappe.db.set_value("Student Group", group, "ms_bell_schedule", name or None)
	frappe.db.commit()
	return {
		"programs": len(chosen_programs),
		"studentGroups": len(chosen_groups),
		"message_ar": "تم تحديث الإسناد."
		if name
		else "تم إلغاء الإسناد — ستتبع التوقيت الافتراضي.",
		"message_en": "Assignment updated.",
	}


# --- Making the saved week follow the schedule --------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def apply_times(name: str, dry_run: int = 1, persona: str = None):
	"""Re-time the saved week of every section that follows this schedule.

	Changing a schedule changes what the school day *is*; the weeks already
	built still carry the old times until they are told otherwise. This is
	that telling — and it is shown first and applied second, because it
	rewrites lessons.

	A lesson with a register taken or a substitution recorded is left alone:
	it is a record of what happened, and moving it would falsify it. Those
	are reported rather than silently skipped.
	"""
	from match_schools.api.timetable_grid import _protected

	if not frappe.db.exists("MS Bell Schedule", name):
		return fail("Not found", "التوقيت غير موجود.")

	clock = {p["order"]: p for p in _all_rows().get(name, []) if not p["isBreak"]}
	groups = [g for g, s in clocks_owner(name).items()]
	if not groups:
		return {
			"slots": 0, "lessons": 0, "protected": 0, "missing": [], "groups": 0,
			"message_ar": "لا توجد شُعب مسنَدة لهذا التوقيت.",
			"message_en": "No sections follow this schedule.",
		}

	slots = frappe.get_all(
		"MS Timetable Slot",
		filters={"student_group": ["in", groups]},
		fields=["name", "student_group", "period_order", "from_time", "to_time", "day"],
		limit_page_length=0,
	)

	changes: list[dict] = []
	missing: list[str] = []
	for s in slots:
		period = clock.get(cint(s.period_order))
		if not period:
			label = f"{s.student_group}: الحصة {cint(s.period_order)}"
			if label not in missing:
				missing.append(label)
			continue
		start, end = f"{period['from']}:00", f"{period['to']}:00"
		if sched.hhmmss(s.from_time) != start or sched.hhmmss(s.to_time) != end:
			changes.append({
				"slot": s.name,
				"group": s.student_group,
				"day": s.day,
				"order": cint(s.period_order),
				"was": f"{sched.hhmmss(s.from_time)[:5]}–{sched.hhmmss(s.to_time)[:5]}",
				"now": f"{period['from']}–{period['to']}",
				"from": start,
				"to": end,
			})

	# The dated lessons that came from those slots, matched on the old time so
	# a lesson someone moved by hand is left where they put it.
	lessons = []
	if changes:
		by_group: dict[str, list[dict]] = {}
		for c in changes:
			by_group.setdefault(c["group"], []).append(c)
		for group, group_changes in by_group.items():
			rows = frappe.get_all(
				"Course Schedule",
				filters={
					"student_group": group,
					"docstatus": ["<", 2],
					"schedule_date": [">=", frappe.utils.today()],
				},
				fields=["name", "schedule_date", "from_time", "to_time"],
				limit_page_length=0,
			)
			for r in rows:
				weekday = frappe.utils.getdate(r.schedule_date).strftime("%A")
				for c in group_changes:
					if c["day"] != weekday:
						continue
					if sched.hhmmss(r.from_time) == sched.hhmmss(c["was"][:5] + ":00"):
						lessons.append({"lesson": r.name, "from": c["from"], "to": c["to"]})
						break

	keep = _protected([l["lesson"] for l in lessons])
	lessons = [l for l in lessons if l["lesson"] not in keep]

	if cint(dry_run):
		return {
			"groups": len(groups),
			"slots": len(changes),
			"lessons": len(lessons),
			"protected": len(keep),
			"missing": missing[:10],
			"sample": changes[:8],
			# Who is affected, by name, so nobody is surprised by the count.
			"sections": sorted({c["group"] for c in changes}),
			"message_ar": "معاينة فقط — لم يُحفَظ شيء.",
			"message_en": "Preview only.",
		}

	for c in changes:
		frappe.db.set_value(
			"MS Timetable Slot", c["slot"], {"from_time": c["from"], "to_time": c["to"]},
			update_modified=False,
		)
	for l in lessons:
		frappe.db.set_value(
			"Course Schedule", l["lesson"], {"from_time": l["from"], "to_time": l["to"]},
			update_modified=False,
		)
	frappe.db.commit()
	return {
		"groups": len(groups),
		"slots": len(changes),
		"lessons": len(lessons),
		"protected": len(keep),
		"missing": missing[:10],
		"message_ar": "تم تطبيق الأوقات على {0} حصة في الجدول و{1} حصة مجدولة.".format(
			len(changes), len(lessons)
		),
		"message_en": "Times applied.",
	}


def clocks_owner(schedule: str) -> dict:
	"""Every section *assigned* to this schedule — directly or through its grade.

	The default is deliberately not counted. "Default" means "for sections
	nobody has assigned"; treating those as followers made a default schedule
	own the whole school, and applying it re-timed every grade onto one bell.
	"""
	sections, programs, _ = _assignments()
	groups = frappe.get_all(
		"Student Group", filters={"disabled": 0}, fields=["name", "program"],
		limit_page_length=0,
	)
	out = {}
	for g in groups:
		owner = sections.get(g.name) or programs.get(g.program)
		if owner == schedule:
			out[g.name] = owner
	return out
