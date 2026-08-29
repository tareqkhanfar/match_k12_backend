"""A teacher's request for the office to print something."""

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class MSPrintRequest(Document):
	def validate(self):
		if cint(self.copies) < 1:
			self.copies = 1
		# A request with nothing to print is a message, not a job. Catching it
		# here means the office never opens an empty envelope.
		if not self.attachments:
			frappe.throw(
				frappe._("Attach at least one file to print."),
				frappe.ValidationError,
			)
