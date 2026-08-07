# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class MSGradeScheme(Document):
	def validate(self):
		self.validate_weights()
		self.validate_single_default()

	def validate_weights(self):
		"""Non-bonus components must add up to exactly 100%."""
		if not self.components:
			frappe.throw(_("Add at least one component."))

		graded = [c for c in self.components if c.component_type != "Bonus"]
		total = sum(flt(c.weight) for c in graded)
		self.total_weight = total

		if not graded:
			frappe.throw(_("A scheme needs at least one non-bonus component."))

		# Allow a rounding cent either way.
		if abs(total - 100) > 0.01:
			frappe.throw(
				_("Component weights must total 100%. They currently total {0}%.").format(
					round(total, 2)
				)
			)

		for c in self.components:
			if flt(c.max_score) <= 0:
				frappe.throw(_("Max score must be greater than zero for {0}.").format(c.component_name))

	def validate_single_default(self):
		"""Only one default scheme per course/program combination."""
		if not self.is_default:
			return
		clash = frappe.db.exists(
			"MS Grade Scheme",
			{
				"is_default": 1,
				"course": self.course or "",
				"program": self.program or "",
				"name": ["!=", self.name],
			},
		)
		if clash:
			frappe.throw(_("Another default scheme already exists for this course and program."))
