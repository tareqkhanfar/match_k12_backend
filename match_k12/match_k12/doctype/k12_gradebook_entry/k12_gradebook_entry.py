# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today


class K12GradebookEntry(Document):
	def validate(self):
		self.set_defaults()
		self.validate_score()
		self.compute_percentage()

	def set_defaults(self):
		if not self.entry_date:
			self.entry_date = today()
		if not self.entered_by:
			self.entered_by = frappe.session.user
		if self.component_type == "Bonus":
			self.is_bonus = 1
		# Fill the group and program from the student's enrolment when missing.
		if self.student and not self.program:
			enrolment = frappe.get_all(
				"Program Enrollment",
				filters={"student": self.student, "docstatus": ["<", 2]},
				fields=["program"],
				order_by="creation desc",
				limit=1,
			)
			if enrolment:
				self.program = enrolment[0].program

	def validate_score(self):
		if flt(self.max_score) <= 0:
			frappe.throw(_("Max score must be greater than zero."))
		if flt(self.score) < 0:
			frappe.throw(_("Score cannot be negative."))
		# A bonus may exceed its own maximum; a normal mark may not.
		if not self.is_bonus and flt(self.score) > flt(self.max_score):
			frappe.throw(
				_("Score cannot be greater than the maximum of {0}.").format(flt(self.max_score))
			)

	def compute_percentage(self):
		self.percentage = (
			round(flt(self.score) / flt(self.max_score) * 100, 2) if flt(self.max_score) else 0
		)
