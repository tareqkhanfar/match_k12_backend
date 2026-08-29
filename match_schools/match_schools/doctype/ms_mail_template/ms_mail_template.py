"""A message a teacher writes often enough to keep.

Placeholders are filled in when the template is applied, not stored resolved:
the same "غياب اليوم" template is used for a different pupil every time.
"""

from frappe.model.document import Document


class MSMailTemplate(Document):
	pass
