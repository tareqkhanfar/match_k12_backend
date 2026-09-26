# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSHelpArticle(Document):
	"""One entry in the help centre: a how-to video or a question with its
	answer, shown to the personas it is written for.

	Only the Administrator account writes these — they are the vendor's
	guidance on using the system, not school content — and everyone reads
	them through `api.help_center`.
	"""

	pass
