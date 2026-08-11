# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSTermQuarter(Document):
	"""One quarter of an academic term, and the marks it is worth.

	A school splits a term into parts — commonly 40 and 60 — and every subject's
	assessment plan is written against those parts. Kept as a child table on
	Academic Term so the division is defined once and every plan reads it.
	"""

	pass
