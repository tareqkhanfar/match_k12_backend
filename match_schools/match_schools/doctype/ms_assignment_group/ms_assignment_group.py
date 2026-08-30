"""One class a piece of homework was set for.

The same worksheet usually goes to every section a teacher takes. The original
single `student_group` field stays as the first of these, so older screens and
records keep working.
"""

from frappe.model.document import Document


class MSAssignmentGroup(Document):
	pass
