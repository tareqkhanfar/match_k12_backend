"""Aim a survey at particular classes, and make one compulsory.

Two additions.

**Classes as an audience.** A survey could be aimed at Students, Teachers,
Parents or All. `student_group` already existed on the doctype but nothing
read it, so a survey written for one class reached the whole school. The
audience gains "Classes", and `ms_student_groups` holds the sections it is
meant for — leaving it empty means every class, which is what a school means
by "aim it at the classes" without naming any.

**Compulsory surveys.** `ms_is_required` marks a survey the portal will not
let past: until the person answers it, every screen shows it instead. Schools
use this for the consent forms and start-of-term declarations that otherwise
take six reminders to collect.

Both default to the behaviour that existed before, so nothing already written
changes.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"MS Survey": [
				{
					"fieldname": "ms_student_groups",
					"label": "Classes",
					"fieldtype": "Small Text",
					"insert_after": "student_group",
					"description": (
						"Comma-separated Student Group names when the audience is "
						"Classes. Empty means every class."
					),
				},
				{
					"fieldname": "ms_is_required",
					"label": "Compulsory",
					"fieldtype": "Check",
					"default": "0",
					"insert_after": "anonymous",
					"description": (
						"The portal will not open until this survey is answered."
					),
				},
			],
		},
		ignore_validate=True,
	)

	# "Classes" has to be a choice before anyone can pick it. Editing the
	# DocType directly is refused for a shipped doctype on a live site, so the
	# option list is extended with a Property Setter — the supported way, and
	# one that survives a future app update rather than being overwritten.
	field = frappe.get_meta("MS Survey").get_field("audience")
	options = [o for o in (field.options or "").split("\n") if o]
	if "Classes" not in options:
		# Kept next to Students: both are aimed at children, one narrower.
		options.insert(1, "Classes")
		frappe.make_property_setter(
			{
				"doctype": "MS Survey",
				"fieldname": "audience",
				"property": "options",
				"value": "\n".join(options),
				"property_type": "Text",
			},
			is_system_generated=False,
		)

	frappe.db.commit()
