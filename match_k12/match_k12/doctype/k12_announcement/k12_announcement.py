# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today


class K12Announcement(Document):
	def validate(self):
		if not self.posted_on:
			self.posted_on = today()
		if self.expires_on and getdate(self.expires_on) < getdate(self.posted_on):
			frappe.throw(_("Expiry date cannot be before the posting date."))
		self.validate_audience_target()

	def validate_audience_target(self):
		if self.audience == "Program" and not self.program:
			frappe.throw(_("Select a Program for a program-targeted announcement."))
		if self.audience == "Student Group" and not self.student_group:
			frappe.throw(_("Select a Student Group for a group-targeted announcement."))
