# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class MSMessage(Document):
	def validate(self):
		if not self.sent_on:
			self.sent_on = now_datetime()
		if self.sender == self.recipient:
			frappe.throw(_("Sender and recipient cannot be the same user."))

	def after_insert(self):
		# A message that does not continue a thread starts its own.
		if not self.thread:
			self.db_set("thread", self.name, update_modified=False)
