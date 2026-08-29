"""A comment, or a reply to one."""

import frappe
from frappe.model.document import Document


class MSPostComment(Document):
	def validate(self):
		# One level of replies. A thread that nests indefinitely is unreadable
		# on a phone, and a reply to a reply to a reply has no natural place to
		# render.
		if self.parent_comment:
			grandparent = frappe.db.get_value(
				"MS Post Comment", self.parent_comment, "parent_comment"
			)
			if grandparent:
				self.parent_comment = grandparent
