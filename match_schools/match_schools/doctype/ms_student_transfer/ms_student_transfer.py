# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSStudentTransfer(Document):
	"""A pupil leaving this school for another.

	The request is written first (where to, when, why, the certificate's
	details), previewed and printed as the ministry's transfer certificate,
	and only then completed — which is when the chosen effects reach the rest
	of the system. What each effect changed is kept on the request, so a
	transfer cancelled by mistake can be put back exactly.
	"""

	pass
