# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class K12AlertRule(Document):
	def validate(self):
		# A rule with no action would match students and do nothing.
		if self.enabled and not self.actions_table:
			frappe.throw(frappe._("Add at least one action, or disable the rule."))
