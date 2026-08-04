# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt

# Rating bands, by weighted percentage.
BANDS = [
	(90, "متميز"),
	(80, "جيد جداً"),
	(70, "جيد"),
	(60, "مقبول"),
	(0, "يحتاج تطوير"),
]


class K12TeacherObservation(Document):
	def validate(self):
		self.calculate()

	def calculate(self):
		"""Weighted percentage across the criteria that were actually scored."""
		earned = 0.0
		possible = 0.0
		for row in self.criteria:
			max_score = flt(row.max_score)
			if not max_score:
				continue
			weight = flt(row.weight) or 1
			earned += (flt(row.score) / max_score) * weight
			possible += weight

		self.overall_score = round(earned, 2)
		self.overall_percent = round(earned / possible * 100, 1) if possible else 0.0
		self.rating = next(
			label for threshold, label in BANDS if self.overall_percent >= threshold
		)
