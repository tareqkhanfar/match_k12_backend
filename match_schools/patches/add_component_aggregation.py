"""How a category combines the assessments inside it.

The tree patch deliberately left this open: a category holds several
assessments, but nothing said how they add up. Summing them is only one
answer. A teacher who sets four short tests and tells the class "I'll count
your best three" needs the plan to say so, and a category marked out of 100
while its assessments total 45 needs to be scaled rather than read literally.

`ms_aggregation` records the choice on the category itself, so it survives the
term and reaches the report card the same way the weight does:

- `sum`      — add the assessments up (the behaviour before this field, and
               the default, so existing plans are unchanged)
- `average`  — the mean of their percentages, for assessments of unequal size
- `best_n`   — the highest `ms_aggregation_n` of them
- `worst_drop` — everything except the lowest `ms_aggregation_n`

`ms_aggregation_n` only means anything for the last two.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"MS Grade Scheme Component": [
				{
					"fieldname": "ms_aggregation",
					"label": "How the assessments combine",
					"fieldtype": "Select",
					"options": "\n".join(["sum", "average", "best_n", "worst_drop"]),
					"default": "sum",
					"insert_after": "ms_quarter",
					"description": (
						"Only for a category. sum: add them up. average: mean of "
						"their percentages. best_n: keep the highest N. "
						"worst_drop: drop the lowest N."
					),
				},
				{
					"fieldname": "ms_aggregation_n",
					"label": "N",
					"fieldtype": "Int",
					"default": "0",
					"insert_after": "ms_aggregation",
					"description": "How many to keep (best_n) or drop (worst_drop).",
				},
			],
		},
		ignore_validate=True,
	)

	# Existing categories keep doing exactly what they did before.
	frappe.db.sql(
		"""
		update `tabMS Grade Scheme Component`
		set ms_aggregation = 'sum'
		where ifnull(ms_aggregation, '') = ''
		"""
	)
	frappe.db.commit()
