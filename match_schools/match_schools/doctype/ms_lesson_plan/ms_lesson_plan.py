"""What a teacher prepared for one lesson."""

import frappe
from frappe.model.document import Document


class MSLessonPlan(Document):
	def validate(self):
		# The plan describes one lesson and is found by it, so a second plan
		# for the same lesson would make "the preparation for Sunday period 1"
		# ambiguous — and the mark on the timetable cell meaningless.
		if self.course_schedule:
			clash = frappe.db.get_value(
				"MS Lesson Plan",
				{"course_schedule": self.course_schedule, "name": ["!=", self.name or ""]},
				"name",
			)
			if clash:
				frappe.throw(
					frappe._("A lesson plan already exists for this lesson."),
					frappe.DuplicateEntryError,
				)
