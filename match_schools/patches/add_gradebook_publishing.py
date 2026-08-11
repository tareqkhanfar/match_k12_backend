"""Let a teacher enter marks without the class seeing them immediately.

Marks used to become visible to students the moment they were saved, which
made the gradebook unusable as a working surface: a teacher marking thirty
papers over an evening had every intermediate state on show, and a typo was
public until it was noticed.

`ms_is_published` makes saving a draft by default. The teacher publishes when
the component is finished, and can unpublish to correct something. Existing
entries are marked published so nothing already visible disappears.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"MS Gradebook Entry": [
				{
					"fieldname": "ms_is_published",
					"label": "Published to students",
					"fieldtype": "Check",
					"default": "0",
					"insert_after": "remarks",
					"description": (
						"When off, only staff can see this mark. Students and "
						"guardians see it once the teacher publishes."
					),
				},
				{
					"fieldname": "ms_published_on",
					"label": "Published on",
					"fieldtype": "Datetime",
					"insert_after": "ms_is_published",
					"read_only": 1,
					"depends_on": "ms_is_published",
				},
			]
		},
		ignore_validate=True,
	)

	# Anything that already exists was visible under the old behaviour;
	# leaving it as a draft would hide marks families have already seen.
	frappe.db.sql(
		"""
		UPDATE `tabMS Gradebook Entry`
		   SET ms_is_published = 1,
		       ms_published_on = IFNULL(ms_published_on, modified)
		 WHERE IFNULL(ms_is_published, 0) = 0
		"""
	)
	frappe.db.commit()
