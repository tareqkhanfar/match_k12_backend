"""Let one behaviour record carry more than one category.

A single incident is often several things at once — late arrival *and* no
homework — and forcing one category meant either losing information or writing
two records for the same event.

The field becomes free text holding a comma-separated list. A Property Setter
rather than an edit to the doctype, because MS Behaviour Record ships with the
app and a direct change would be overwritten on the next install. Existing
single values remain valid: "Disruption" is a one-item list.
"""

import frappe


def execute():
	meta = frappe.get_meta("MS Behaviour Record")
	field = meta.get_field("category")
	if not field or field.fieldtype != "Select":
		return

	# Keep the options as the picker's source; the frontend reads them to build
	# its multi-select, and the desk still offers them as a datalist.
	frappe.make_property_setter(
		{
			"doctype": "MS Behaviour Record",
			"doctype_or_field": "DocField",
			"fieldname": "category",
			"property": "fieldtype",
			"value": "Small Text",
			"property_type": "Data",
		},
		ignore_validate=True,
	)
	frappe.clear_cache(doctype="MS Behaviour Record")
	frappe.db.commit()
