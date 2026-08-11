"""Quarters on the term, and a tree-shaped assessment plan.

Two changes, both additive.

**Quarters.** A term is divided into quarters — typically Q1 worth 40 marks and
Q2 worth 60 — configured on the Academic Term itself. This is only about the
assessment plan; nothing else in the system starts filtering by quarter.

**The tree.** A plan used to be a flat list of components whose weights summed
to 100. Schools do not work that way: they define a category ("امتحانات
يومية", 20%) and then hold several assessments inside it, each with its own
marks. Only the category's weight reaches the final 100.

`ms_parent_component` turns the existing component table into that tree — a row
with no parent is a category, a row naming one is an assessment inside it. The
flat plans already in use remain valid: every one of their rows is a category
with no children, which is exactly what they were before.

Nothing here decides how a category's children are combined. The teacher sets
that during the term (best 3 of 4, and so on), because it depends on how the
term actually went.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Academic Term": [
				{
					"fieldname": "ms_quarters_section",
					"label": "Quarters",
					"fieldtype": "Section Break",
					"insert_after": "term_end_date",
				},
				{
					"fieldname": "ms_quarters",
					"label": "Quarters",
					"fieldtype": "Table",
					"options": "MS Term Quarter",
					"insert_after": "ms_quarters_section",
					"description": (
						"How this term is divided, and how many marks each part "
						"is worth. Example: Q1 = 40, Q2 = 60."
					),
				},
			],
			"MS Grade Scheme Component": [
				{
					"fieldname": "ms_parent_component",
					"label": "Inside category",
					"fieldtype": "Data",
					"insert_after": "component_name",
					"description": (
						"Leave empty for a category. Set to a category's name to "
						"make this one of the assessments inside it."
					),
				},
				{
					"fieldname": "ms_quarter",
					"label": "Quarter",
					"fieldtype": "Data",
					"insert_after": "ms_parent_component",
					"description": "Which quarter of the term this belongs to.",
				},
			],
		},
		ignore_validate=True,
	)
	frappe.db.commit()
