# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Give back to each section the plan it had before another section's sheet
changed it.

Until 2026-10 a teacher's edit from one section's marks sheet — a new
assessment, a quiz now out of 5 — was written into the grade's shared plan, so
it reached every section of the grade; and a missing mark counts as zero. From
now on such edits are the section's own (`api/section_plan.py`). This patch
repairs what had already leaked, without moving a single mark:

- An assessment a teacher added stays in the sections that teacher teaches and
  in any section that already has marks on it; it leaves the others.
- A maximum a teacher changed stays for the sections that teacher teaches and
  for any section that has marks on that assessment; the others get back the
  original (the plan template's figure, else what its sibling assessments are
  out of). A section with marks is never changed, so no mark changes meaning.
- Changes by the Administrator are not guessed at; they are only reported.

Everything done or left is recorded under the default `ms_plan_scope_repair_v1`.
`repair(dry_run=True)` reports without writing.
"""

import json
from collections import Counter, defaultdict

import frappe
from frappe.utils import flt, now_datetime

KEY = "ms_plan_scope_repair_v1"


def execute():
	if frappe.db.get_default(KEY):
		return
	frappe.reload_doc("match_schools", "doctype", "ms_section_plan_item")
	report = repair(dry_run=False)
	frappe.db.set_default(KEY, json.dumps(report, ensure_ascii=False, default=str))
	for line in report["lines"]:
		print(line)


def _instructor(user: str) -> str | None:
	from match_schools.api.utils import get_linked_instructor

	if not user or user in ("Administrator", "Guest"):
		return None
	try:
		return get_linked_instructor(user)
	except Exception:
		return None


def repair(dry_run: bool = True) -> dict:
	from match_schools.api import section_plan

	lines: list[str] = []
	done: list[dict] = []
	schemes = {
		s.name: s
		for s in frappe.get_all("MS Grade Scheme", fields=["name", "course", "program"], limit_page_length=0)
	}
	rows_of: dict[str, list] = defaultdict(list)
	for r in frappe.get_all(
		"MS Grade Scheme Component",
		filters={"parenttype": "MS Grade Scheme"},
		fields=[
			"name", "parent", "component_name", "component_type", "ms_parent_component",
			"ms_quarter", "max_score", "modified", "modified_by",
		],
		order_by="idx",
		limit_page_length=0,
	):
		rows_of[r.parent].append(r)

	templates: dict[tuple, set] = defaultdict(set)
	if frappe.db.table_exists("MS Assessment Plan Template"):
		for t in frappe.get_all("MS Assessment Plan Template", fields=["plan"], limit_page_length=0):
			try:
				plan = json.loads(t.plan or "[]")
			except Exception:
				continue
			for cat in plan if isinstance(plan, list) else []:
				for ch in cat.get("children") or []:
					templates[((cat.get("name") or "").strip(), (ch.get("name") or "").strip())].add(
						flt(ch.get("maxScore"))
					)

	def label(group):
		return frappe.db.get_value("Student Group", group, "student_group_name") or group

	def sections(scheme):
		s = schemes[scheme]
		from match_schools.api.utils import apply_period

		# Every section of this year that studies the course: its grade's
		# sections (the plan's programme, or any programme listing the
		# course), and whoever the timetable gives it to. Last year's 4-ب is
		# not another class sitting this plan.
		programs = {s.program} if s.program else set(
			frappe.get_all("Program Course", filters={"course": s.course}, pluck="parent", distinct=True)
		)
		groups = set()
		if programs:
			groups |= set(
				frappe.get_all(
					"Student Group",
					filters=apply_period({"program": ["in", list(programs)], "disabled": 0}, "Student Group"),
					pluck="name",
				)
			)
		groups |= set(
			frappe.get_all("MS Timetable Slot", filters={"course": s.course, "active": 1}, pluck="student_group", distinct=True)
		)
		groups |= set(
			frappe.get_all("MS Gradebook Entry", filters={"course": s.course}, pluck="student_group", distinct=True)
		)
		return {g for g in groups if g}

	def taught(instructor, course):
		if not instructor:
			return set()
		out = set(
			frappe.get_all(
				"MS Timetable Slot",
				filters={"course": course, "instructor": instructor, "active": 1},
				pluck="student_group",
				distinct=True,
			)
		)
		out |= set(
			frappe.get_all(
				"Course Schedule",
				filters={"course": course, "instructor": instructor, "docstatus": ["<", 2]},
				pluck="student_group",
				distinct=True,
			)
		)
		return {g for g in out if g}

	def marked(course, component):
		return Counter(
			frappe.get_all(
				"MS Gradebook Entry",
				filters={"course": course, "component_name": component},
				pluck="student_group",
				limit_page_length=0,
			)
		)

	# --- assessments added from a sheet ------------------------------------
	adds = []
	for v in frappe.get_all(
		"Version",
		filters={"ref_doctype": "MS Grade Scheme"},
		fields=["docname", "owner", "creation", "data"],
		order_by="creation",
		limit_page_length=0,
	):
		try:
			data = json.loads(v.data or "{}")
		except Exception:
			continue
		added = data.get("added") or []
		if len(added) == 1 and not data.get("removed") and v.docname in schemes:
			row = added[0][1] if isinstance(added[0], list) and len(added[0]) > 1 else {}
			if row.get("ms_parent_component"):
				adds.append((v, row))
	add_stamps = {(v.docname, v.owner, v.creation.replace(microsecond=0)) for v, _ in adds}

	for v, row in adds:
		scheme = v.docname
		course = schemes[scheme].course
		name = row.get("component_name")
		current = next((r for r in rows_of[scheme] if r.component_name == name and r.ms_parent_component), None)
		if not current:
			continue
		if v.owner == "Administrator":
			lines.append(f"تُرك كما هو (أضافه Administrator): {course} — «{name}» تحت «{current.ms_parent_component}».")
			continue
		everyone = sections(scheme)
		marks = marked(course, name)
		keep = taught(_instructor(v.owner), course) | {g for g in marks if marks[g]}
		keep &= everyone or keep
		if not keep or keep >= everyone:
			continue
		gone = sorted(everyone - keep)
		lines.append(
			f"{course} — «{name}» (أضافه {v.owner}): بقي لـ {', '.join(label(g) for g in sorted(keep))}"
			f" وأُزيل من {', '.join(label(g) for g in gone)} (لا علامات فيها)."
		)
		done.append({"kind": "assessment", "scheme": scheme, "component": name, "keep": sorted(keep), "removedFrom": gone})
		if dry_run:
			continue
		for g in keep:
			if not frappe.db.exists(
				section_plan.DOCTYPE, {"grade_scheme": scheme, "student_group": g, "component_name": name}
			):
				section_plan.put(
					scheme, g, "assessment", name,
					parent_component=current.ms_parent_component,
					component_type=current.component_type,
					quarter=current.ms_quarter,
					max_score=flt(current.max_score),
					notes="نُقل من الخطة المشتركة: أُضيف من ورقة علامات شعبة.",
				)
		doc = frappe.get_doc("MS Grade Scheme", scheme)
		doc.components = [c for c in doc.components if c.name != current.name]
		for i, c in enumerate(doc.components, start=1):
			c.idx = i
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
		rows_of[scheme] = [r for r in rows_of[scheme] if r.name != current.name]

	# --- maximums changed from a sheet -------------------------------------
	for scheme, rows in rows_of.items():
		if scheme not in schemes:
			continue
		course = schemes[scheme].course
		for r in rows:
			if not r.ms_parent_component or r.modified_by in ("Administrator", None, ""):
				continue
			stamp = r.modified.replace(microsecond=0)
			if any(
				d == scheme and o == r.modified_by and abs((t - stamp).total_seconds()) <= 3
				for d, o, t in add_stamps
			):
				# Rewritten as a side effect of an addition, not edited.
				continue
			instructor = _instructor(r.modified_by)
			if not instructor:
				continue
			current = flt(r.max_score)
			from_template = templates.get((r.ms_parent_component.strip(), r.component_name.strip())) or set()
			if len(from_template) == 1:
				original = next(iter(from_template))
			else:
				siblings = Counter(
					flt(x.max_score)
					for x in rows
					if x.ms_parent_component == r.ms_parent_component and x.name != r.name and x.modified_by == "Administrator"
				)
				original = siblings.most_common(1)[0][0] if siblings else 0
			if not original or original == current:
				continue
			everyone = sections(scheme)
			marks = marked(course, r.component_name)
			keep = (taught(instructor, course) | {g for g in marks if marks[g]}) & everyone
			back = sorted(everyone - keep)
			if not back:
				continue
			lines.append(
				f"{course} — «{r.component_name}» غيّره {r.modified_by} إلى {current:g}: بقي {current:g} لـ "
				f"{', '.join(label(g) for g in sorted(keep)) or '—'} وعاد {original:g} لـ {', '.join(label(g) for g in back)}."
			)
			done.append({
				"kind": "max", "scheme": scheme, "component": r.component_name,
				"current": current, "original": original, "keep": sorted(keep), "restored": back,
			})
			if dry_run:
				continue
			for g in keep:
				section_plan.put(
					scheme, g, "override", r.component_name, max_score=current,
					notes="علامة عظمى عُدّلت من ورقة علامات — بقيت لهذه الشعبة.",
				)
			frappe.db.set_value("MS Grade Scheme Component", r.name, "max_score", original)

	if not lines:
		lines.append("لا تعديلات من أوراق العلامات تسرّبت إلى شعب أخرى.")
	if not dry_run:
		frappe.db.commit()
	return {"at": str(now_datetime()), "dryRun": dry_run, "lines": lines, "changes": done}
