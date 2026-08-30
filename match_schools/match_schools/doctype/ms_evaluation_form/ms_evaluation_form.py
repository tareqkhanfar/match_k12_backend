"""A form a school defines for itself, and then fills in for pupils.

Schools do not agree on what to assess or how. One wants "الاستماع / الأكل في
الحصة" answered دائماً–أحياناً–أبداً; another wants a KG domain sheet; a third
wants marks out of ten. Hard-coding any of those means the next school is
served by nobody, so the form carries its own criteria and its own scale, and
the screens read both rather than assuming either.

`scale_type` decides how an answer is captured. `scale` supplies the words for
a مقياس form, each carrying a score so a form built out of words can still be
totalled.
"""

from frappe.model.document import Document


class MSEvaluationForm(Document):
	pass
