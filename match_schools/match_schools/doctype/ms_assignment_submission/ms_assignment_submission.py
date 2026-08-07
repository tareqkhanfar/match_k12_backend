# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, getdate, now_datetime


class MSAssignmentSubmission(Document):
	def validate(self):
		self.set_submitted_on()
		self.validate_unique_submission()
		self.validate_score()
		self.set_late_status()

	def set_submitted_on(self):
		if not self.submitted_on:
			self.submitted_on = now_datetime()

	def validate_unique_submission(self):
		existing = frappe.db.exists(
			"MS Assignment Submission",
			{
				"assignment": self.assignment,
				"student": self.student,
				"name": ["!=", self.name],
			},
		)
		if existing:
			frappe.throw(
				_("This student already has a submission for {0}.").format(self.assignment)
			)

	def validate_score(self):
		if self.score in (None, ""):
			return
		maximum = self.maximum_score or frappe.db.get_value(
			"MS Assignment", self.assignment, "maximum_score"
		)
		if maximum and self.score > maximum:
			frappe.throw(_("Score cannot be greater than the maximum score ({0}).").format(maximum))
		if self.score < 0:
			frappe.throw(_("Score cannot be negative."))

	def set_late_status(self):
		"""Flag a submission that arrived after the due date."""
		if self.status not in ("Submitted", "Late"):
			return
		due_date = frappe.db.get_value("MS Assignment", self.assignment, "due_date")
		if due_date and self.submitted_on:
			if getdate(get_datetime(self.submitted_on)) > getdate(due_date):
				self.status = "Late"
