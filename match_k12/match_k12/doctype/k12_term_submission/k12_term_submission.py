# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class K12TermSubmission(Document):
	def validate(self):
		# One submission per class/course/term, so the workflow has a single
		# authoritative record to move through its states.
		existing = frappe.db.exists(
			"K12 Term Submission",
			{
				"student_group": self.student_group,
				"course": self.course,
				"academic_term": self.academic_term,
				"name": ["!=", self.name],
			},
		)
		if existing:
			frappe.throw(
				frappe._("Marks for this class and subject have already been submitted ({0}).").format(
					existing
				)
			)
