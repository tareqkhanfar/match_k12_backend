# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSAdmissionRequest(Document):
	"""A family's request to join the school, before it is an application.

	The office writes down who the child is and what they want, confirms it,
	and prints the admission-approval letter («متسع»). Only then is it moved
	on as a Student Applicant — which is where accepting or refusing happens.
	"""

	pass
