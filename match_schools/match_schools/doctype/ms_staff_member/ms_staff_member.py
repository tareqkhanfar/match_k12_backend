# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MSStaffMember(Document):
	"""The file of a member of the office staff — a secretary, for now.

	Teachers have Instructor and students have Student; the office had only a
	bare User with a role, so there was nowhere to keep who they are, their
	papers, or to hand them a new password the way every other account gets
	one. The account stays the User; this is the person behind it.
	"""

	def on_update(self):
		# An inactive member cannot sign in: the file, not a separate switch,
		# is where the office records that someone has left.
		if self.user and frappe.db.exists("User", self.user):
			enabled = 1 if self.status == "Active" else 0
			if frappe.db.get_value("User", self.user, "enabled") != enabled:
				frappe.db.set_value("User", self.user, "enabled", enabled)
