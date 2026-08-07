# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class MSAssignment(Document):
	def validate(self):
		self.validate_dates()

	def validate_dates(self):
		if self.assigned_on and self.due_date and getdate(self.due_date) < getdate(self.assigned_on):
			frappe.throw(_("Due Date cannot be before the Assigned On date."))

	@property
	def submission_counts(self) -> dict:
		"""Submitted / total counts used by the frontend cards."""
		total = frappe.db.count(
			"Student Group Student",
			{"parent": self.student_group, "parenttype": "Student Group", "active": 1},
		)
		submitted = frappe.db.count(
			"MS Assignment Submission",
			{"assignment": self.name, "docstatus": ["<", 2]},
		)
		return {"submitted": submitted, "total": total}
