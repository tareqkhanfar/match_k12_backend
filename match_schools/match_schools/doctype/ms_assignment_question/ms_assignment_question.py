"""A pupil asking about a piece of homework, and the teacher answering.

Kept beside the homework rather than sent as a message: the next pupil with
the same question should be able to read the answer, which is what `is_public`
is for.
"""

from frappe.model.document import Document


class MSAssignmentQuestion(Document):
	pass
