# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSAgendaType(Document):
	"""A kind of agenda event and what it does to the school day: a holiday closes
	the school, a trip suspends a section's lessons, an exam period shows in exam
	scheduling, a meeting is the staff's."""

	pass
