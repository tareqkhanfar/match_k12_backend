# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSCustomPage(Document):
	"""A page generated from a form template: its list of filled copies, a
	filling screen and printing — with per-page field rules (hidden, read
	only, required, defaults, values fetched from the student's record)."""

	pass
