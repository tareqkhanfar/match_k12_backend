"""Let a teacher drop a single assessment from the calculation.

A quiz goes wrong — the wrong paper is handed out, half the class is on a trip
— and the school decides it does not count. Until now the only ways out were
deleting the marks, which loses what the students actually scored, or leaving
it in and distorting the term.

`ms_excluded` keeps the marks on the record and takes them out of the sum. It
lives on the gradebook entry rather than the plan because the decision is
about this class's sitting of the assessment, not about the plan every class
shares.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"MS Gradebook Entry": [
				{
					"fieldname": "ms_excluded",
					"label": "Excluded from the total",
					"fieldtype": "Check",
					"default": "0",
					"insert_after": "is_bonus",
					"description": (
						"The mark stays on the student's record but does not "
						"count toward the subject total."
					),
				}
			]
		},
		ignore_validate=True,
	)
	frappe.clear_cache(doctype="MS Gradebook Entry")
