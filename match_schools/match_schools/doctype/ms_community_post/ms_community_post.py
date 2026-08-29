"""A post on the school's community feed."""

import frappe
from frappe.model.document import Document


class MSCommunityPost(Document):
	def validate(self):
		# The audience decides who can see it, so the target it names has to
		# exist — a Class post with no class would be visible to nobody, and a
		# Student post with no student is a privacy question with no answer.
		if self.audience == "Class" and not self.student_group:
			frappe.throw(
				frappe._("Choose the class this post is for."), frappe.ValidationError
			)
		if self.audience == "Student" and not self.student:
			frappe.throw(
				frappe._("Choose the student this post is about."), frappe.ValidationError
			)
