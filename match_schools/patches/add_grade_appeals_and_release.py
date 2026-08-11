"""Fields for grade appeals and scheduled release of marks.

Two things a school needs after results are published:

  * A student appeals a mark and turns out to be right. The administration has
    to reopen that one course for that one section — not the whole term — and
    afterwards anyone must be able to see exactly what changed and why.
    `ms_reopened_*` records the reopening; the change itself is already in
    Frappe's Version table, so nothing duplicates it.

  * Marks should reach families on a date the school chooses, not the moment a
    teacher finishes marking. `ms_release_on` holds that date; a scheduled job
    publishes the entries when it arrives.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"MS Gradebook Entry": [
				{
					"fieldname": "ms_release_on",
					"label": "Release to families on",
					"fieldtype": "Date",
					"insert_after": "ms_published_on",
					"description": (
						"Leave empty to control visibility manually. When set, the "
						"mark becomes visible to students and guardians on this date."
					),
				},
			],
			"MS Term Submission": [
				{
					"fieldname": "ms_reopened_on",
					"label": "Reopened on",
					"fieldtype": "Datetime",
					"insert_after": "review_notes",
					"read_only": 1,
				},
				{
					"fieldname": "ms_reopened_by",
					"label": "Reopened by",
					"fieldtype": "Link",
					"options": "User",
					"insert_after": "ms_reopened_on",
					"read_only": 1,
				},
				{
					"fieldname": "ms_reopen_reason",
					"label": "Reason for reopening",
					"fieldtype": "Small Text",
					"insert_after": "ms_reopened_by",
					"description": "Why the marks were reopened — usually a student appeal.",
				},
				{
					"fieldname": "ms_release_on",
					"label": "Release results on",
					"fieldtype": "Date",
					"insert_after": "published_on",
					"description": (
						"Results stay hidden from families until this date, even "
						"once approved."
					),
				},
			],
		},
		ignore_validate=True,
	)
	frappe.db.commit()
