# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, today


class K12BehaviourRecord(Document):
	def validate(self):
		if not self.record_date:
			self.record_date = today()
		self.normalise_points()

	def normalise_points(self):
		"""Keep the sign of `points` consistent with the record type so totals
		can simply be summed."""
		points = abs(cint(self.points))
		if not points:
			# Default weight so a record always moves the total.
			points = 1
		self.points = points if self.record_type == "Positive" else -points
