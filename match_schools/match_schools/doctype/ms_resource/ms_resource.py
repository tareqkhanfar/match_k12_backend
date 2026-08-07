# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MSResource(Document):
	def validate(self):
		# A published resource with nothing to open is a dead link for students.
		if self.status == "Published" and not (self.files or self.external_url):
			frappe.throw(
				frappe._("Attach a file or add a link before publishing.")
			)
