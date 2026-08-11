# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSGradeRule(Document):
	"""How one category's assessments are combined, for one class.

	Deliberately not part of the assessment plan. The plan says a category is
	worth 20%; whether that 20% comes from all four daily tests or the best
	three is a decision the teacher makes during the term, once they know how
	the term went — and it can differ between two sections studying the same
	subject.

	`track_changes` is on: a rule that changes every student's mark in a class
	must be answerable months later.
	"""

	pass
