# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MSActivity(Document):
	def validate(self):
		if self.end_date and self.start_date and str(self.end_date) < str(self.start_date):
			frappe.throw(frappe._("The end date cannot be before the start date."))
		if (
			self.registration_deadline
			and self.start_date
			and str(self.registration_deadline) > str(self.start_date)
		):
			frappe.throw(
				frappe._("Registration must close on or before the activity starts.")
			)
