# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSAssessmentPlanTemplate(Document):
	"""A reusable assessment plan: categories and assessments per quarter.

	Quarters are referenced by position, not by name, so one template serves
	every term — "the first quarter" means the same thing in any year even when
	a term names it differently. The totals it was written for are kept so a
	term whose quarters are worth something else can have the weights scaled
	rather than refused.
	"""

	pass
