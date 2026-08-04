# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class K12Quiz(Document):
	def validate(self):
		self.total_marks = sum(flt(q.marks) for q in self.questions)

		if self.opens_on and self.closes_on and str(self.closes_on) <= str(self.opens_on):
			frappe.throw(frappe._("The quiz must close after it opens."))

		# A published quiz with no questions would trap students on an empty page.
		if self.status == "Published" and not self.questions:
			frappe.throw(frappe._("Add at least one question before publishing."))

		for q in self.questions:
			if q.question_type == "Multiple Choice" and not (q.option_a and q.option_b):
				frappe.throw(
					frappe._("Question {0} needs at least two options.").format(q.idx)
				)
