# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MSTimetablePlan(Document):
	def validate(self):
		# A plan that asks for more lessons than the week can hold will never
		# generate cleanly, so say so at save time rather than at generation.
		teaching = [p for p in self.periods if not p.is_break]
		days = [d.strip() for d in (self.working_days or "").split(",") if d.strip()]
		capacity = len(teaching) * len(days)
		demand = sum(int(s.periods_per_week or 0) for s in self.subject_loads)

		if capacity and demand > capacity:
			frappe.throw(
				frappe._(
					"The subjects need {0} periods a week but the timetable only has {1}."
				).format(demand, capacity)
			)
