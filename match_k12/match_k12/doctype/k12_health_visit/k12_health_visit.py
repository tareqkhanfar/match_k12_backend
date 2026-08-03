# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class K12HealthVisit(Document):
	def validate(self):
		if not self.visit_date:
			self.visit_date = now_datetime()
		if not self.attended_by:
			self.attended_by = frappe.session.user
