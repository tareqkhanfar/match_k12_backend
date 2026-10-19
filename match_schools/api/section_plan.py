# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""A section's view of its subject's assessment plan.

The plan (`MS Grade Scheme`) is the subject's for the whole grade. A teacher
working in one section's marks sheet may add an assessment, change what one is
out of, or decide how a category's assessments combine — and that must stay in
that section. Until 2026-10 those edits were written into the shared plan, and
because a missing mark counts as zero, an assessment added in 4-أ lowered every
pupil's total in 4-ب.

So a section's own changes live in `MS Section Plan Item`, and every reader
asks `plan_rows(scheme, student_group)` for the plan as that section sits it:
the shared rows, with the section's overrides applied and its extra
assessments placed beside their siblings. Without a section — the plan editor,
a grade-wide report — the shared plan is returned untouched.
"""

import frappe
from frappe.utils import cint, flt, now_datetime

DOCTYPE = "MS Section Plan Item"
ROW_FIELDS = [
	"name", "component_name", "component_type", "weight", "max_score",
	"ms_parent_component", "ms_quarter", "ms_aggregation", "ms_aggregation_n", "idx",
]


def _shared(scheme: str) -> list:
	return frappe.get_all(
		"MS Grade Scheme Component",
		filters={"parent": scheme, "parenttype": "MS Grade Scheme"},
		fields=ROW_FIELDS,
		order_by="idx",
		limit_page_length=0,
	)


def items(scheme: str, student_group: str | None = None) -> list:
	if not scheme or not frappe.db.table_exists(DOCTYPE):
		return []
	filters = {"grade_scheme": scheme}
	if student_group:
		filters["student_group"] = student_group
	return frappe.get_all(
		DOCTYPE,
		filters=filters,
		fields=[
			"name", "student_group", "kind", "component_name", "parent_component",
			"component_type", "quarter", "max_score", "aggregation", "aggregation_n",
			"set_by", "set_on",
		],
		order_by="creation",
		limit_page_length=0,
	)


def plan_rows(scheme: str, student_group: str | None = None) -> list:
	"""The plan's rows as `student_group` sits them, in plan order.

	Each row is a `frappe._dict` shaped like `MS Grade Scheme Component`, so
	code written against `doc.components` reads it unchanged. A section's own
	assessment carries `ms_section_item`; a row whose maximum or rule this
	section changed carries `ms_section_override`.
	"""
	rows = [frappe._dict(r) for r in _shared(scheme)]
	if not student_group:
		return rows
	mine = items(scheme, student_group)
	if not mine:
		return rows

	by_name = {r.component_name: r for r in rows}
	for item in mine:
		row = by_name.get(item.component_name)
		if item.kind == "override" or row:
			# An override — or an extra assessment the plan has since gained
			# under the same name, which then only keeps its own maximum.
			if not row:
				continue
			if flt(item.max_score) > 0 and row.get("ms_parent_component"):
				row.max_score = flt(item.max_score)
				row.ms_section_override = 1
			if item.aggregation and not row.get("ms_parent_component"):
				row.ms_aggregation = item.aggregation
				row.ms_aggregation_n = cint(item.aggregation_n)
				row.ms_section_override = 1
			continue

		parent = by_name.get(item.parent_component)
		if not parent or parent.get("ms_parent_component"):
			# Its category left the plan: the marks stay, the column goes.
			continue
		extra = frappe._dict(
			name=item.name,
			component_name=item.component_name,
			component_type=item.component_type or parent.component_type or "Exam",
			weight=0,
			max_score=flt(item.max_score),
			ms_parent_component=item.parent_component,
			ms_quarter=item.quarter or parent.get("ms_quarter"),
			ms_aggregation=None,
			ms_aggregation_n=0,
			ms_section_item=1,
		)
		# Beside its siblings: after the last row of its category.
		at = max(
			(i for i, r in enumerate(rows) if r.get("ms_parent_component") == item.parent_component),
			default=rows.index(parent),
		)
		rows.insert(at + 1, extra)
		by_name[extra.component_name] = extra

	for i, r in enumerate(rows, start=1):
		r.idx = i
	return rows


def name_to_category(scheme: str) -> dict[str, str]:
	"""Every assessment's category, across the plan and all sections' own —
	for readers that only know an entry's name, never its section."""
	out = {
		r.component_name: r.ms_parent_component
		for r in _shared(scheme)
		if r.get("ms_parent_component")
	}
	for item in items(scheme):
		if item.kind == "assessment" and item.parent_component:
			out.setdefault(item.component_name, item.parent_component)
	return out


def put(scheme: str, student_group: str, kind: str, component_name: str, **values):
	"""Create or update this section's item for one row of the plan."""
	course = frappe.db.get_value("MS Grade Scheme", scheme, "course")
	existing = frappe.db.get_value(
		DOCTYPE,
		{"grade_scheme": scheme, "student_group": student_group, "component_name": component_name},
		"name",
	)
	doc = frappe.get_doc(DOCTYPE, existing) if existing else frappe.new_doc(DOCTYPE)
	doc.update({
		"grade_scheme": scheme,
		"course": course,
		"student_group": student_group,
		"component_name": component_name,
		"set_by": frappe.session.user,
		"set_on": now_datetime(),
		**values,
	})
	if not existing:
		doc.kind = kind
	doc.save(ignore_permissions=True)
	return doc


def section_changes(scheme: str) -> list[dict]:
	"""Each section's own changes to this plan, for the plan editor."""
	rows = items(scheme)
	if not rows:
		return []
	labels = dict(
		frappe.get_all(
			"Student Group",
			filters={"name": ["in", list({r.student_group for r in rows})]},
			fields=["name", "student_group_name"],
			as_list=True,
		)
	)
	modes = {"sum": "جمع العلامات", "average": "متوسط النسب", "best_n": "أفضل", "worst_drop": "استبعاد أدنى"}
	out = []
	for r in rows:
		if r.kind == "assessment":
			what = f"امتحان إضافي تحت «{r.parent_component}» من {flt(r.max_score):g}"
		elif flt(r.max_score) > 0:
			what = f"العلامة العظمى {flt(r.max_score):g}"
		else:
			what = f"الاحتساب: {modes.get(r.aggregation, r.aggregation)}" + (
				f" {cint(r.aggregation_n)}" if r.aggregation in ("best_n", "worst_drop") else ""
			)
		out.append({
			"id": r.name,
			"studentGroup": r.student_group,
			"section": labels.get(r.student_group, r.student_group),
			"kind": r.kind,
			"component": r.component_name,
			"change": what,
			"setBy": r.set_by,
			"setOn": str(r.set_on or "")[:16],
		})
	return out
