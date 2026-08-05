# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class K12Survey(Document):
	def validate(self):
		if self.status == "Open" and not self.questions:
			frappe.throw(frappe._("Add at least one question before opening the survey."))
		if self.opens_on and self.closes_on and str(self.closes_on) < str(self.opens_on):
			frappe.throw(frappe._("The survey must close after it opens."))
