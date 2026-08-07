# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MSHealthRecord(Document):
	def validate(self):
		# One health record per student; the field is unique but give a clear message.
		existing = frappe.db.exists(
			"MS Health Record", {"student": self.student, "name": ["!=", self.name]}
		)
		if existing:
			frappe.throw(_("A health record already exists for this student."))

		if self.height_cm and self.height_cm < 0:
			frappe.throw(_("Height cannot be negative."))
		if self.weight_kg and self.weight_kg < 0:
			frappe.throw(_("Weight cannot be negative."))
