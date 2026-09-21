"""A report filed by a reader against a piece of content.

Kept as its own record rather than a flag on the content: the content may be
deleted by its author the moment it is reported, and a moderation decision
needs the text that was actually complained about. `snapshot` holds it.
"""

from frappe.model.document import Document


class MSContentReport(Document):
	pass
