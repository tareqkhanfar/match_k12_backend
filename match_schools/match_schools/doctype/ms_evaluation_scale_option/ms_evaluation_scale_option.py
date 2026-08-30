"""One answer a form accepts — "دائماً", "أحياناً", "أبداً".

Each carries a score so a form built from words can still be totalled, and a
tone so the grid can colour an answer without the screen knowing what the
words mean.
"""

from frappe.model.document import Document


class MSEvaluationScaleOption(Document):
	pass
