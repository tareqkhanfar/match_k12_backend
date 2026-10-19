# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSSectionPlanItem(Document):
	"""One section's own change to its subject's assessment plan.

	The plan (`MS Grade Scheme`) belongs to a subject and grade, and every
	section of the grade sits it. What a teacher changes from one section's
	marks sheet — a fifth daily test, a quiz out of 5 instead of 10, best three
	of four — is that section's business only, so it is stored here, beside
	the plan, and laid over it when that section's plan is read
	(`api/section_plan.py`). The plan editor rewrites the plan's own rows and
	never touches these.

	`assessment`: an extra assessment, only in this section, under a category.
	`override`: this section's own maximum for a plan assessment, or its own
	way of combining a category's assessments.
	"""

	pass
