"""دفتر الحصص — what actually happened in one lesson.

The lesson plan says what a teacher intended; this says what the class did.
The two are deliberately separate records: a plan written last week and edited
into a record of the lesson leaves no trace of either.

Written for the family as much as for the school. A pupil who was absent, or a
parent asking "what did you do today", currently depends on the pupil
remembering. `is_published` is on by default for that reason — a log nobody
outside the staffroom can read solves the smaller half of the problem.
"""

from frappe.model.document import Document


class MSClassLog(Document):
	pass
