# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MSActivityEnrolment(Document):
	def validate(self):
		# One registration per student per activity.
		duplicate = frappe.db.exists(
			"MS Activity Enrolment",
			{"activity": self.activity, "student": self.student, "name": ["!=", self.name]},
		)
		if duplicate:
			frappe.throw(frappe._("This student is already registered for the activity."))
