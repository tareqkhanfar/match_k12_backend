"""One line a form asks about — "يستمع لزملائه", "يرتّب أدواته".

`category` groups the lines into the bands the grid shows as merged headers.
It is free text rather than a link: a KG form's domains and a behaviour form's
areas have nothing to do with each other, and a shared list would force one
school's vocabulary onto another's.
"""

from frappe.model.document import Document


class MSEvaluationCriterion(Document):
	pass
