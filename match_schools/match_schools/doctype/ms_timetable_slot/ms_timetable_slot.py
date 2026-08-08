# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MSTimetableSlot(Document):
	"""One recurring lesson in the weekly pattern.

	The pattern is what a school actually designs: "Sunday, period 2, maths
	with Mr Ahmad in room 1". Dated `Course Schedule` rows are generated from
	it, so a change to the pattern can regenerate the term rather than being
	applied to every week by hand.
	"""

	def validate(self):
		self.set_times_from_period()

	def set_times_from_period(self):
		"""Fill the times from the school's period definition when omitted."""
		if self.from_time and self.to_time:
			return
		period = frappe.db.get_value(
			"MS Timetable Period",
			{"period_order": self.period_order},
			["from_time", "to_time"],
			as_dict=True,
		)
		if period:
			self.from_time = self.from_time or period.from_time
			self.to_time = self.to_time or period.to_time
