# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class K12LibraryBook(Document):
	def validate(self):
		if cint(self.total_copies) < 1:
			frappe.throw(_("Total copies must be at least 1."))
		self.sync_available_copies()

	def sync_available_copies(self):
		"""Available = total - copies currently out on loan."""
		on_loan = frappe.db.count(
			"K12 Book Loan", {"book": self.name, "status": ["in", ["Issued", "Overdue"]]}
		)
		available = cint(self.total_copies) - on_loan
		if available < 0:
			frappe.throw(
				_("There are {0} copies on loan, which is more than the total of {1}.").format(
					on_loan, self.total_copies
				)
			)
		self.available_copies = available
