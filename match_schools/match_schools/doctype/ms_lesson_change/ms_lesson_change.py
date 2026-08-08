# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MSLessonChange(Document):
	"""A one-day departure from the weekly pattern.

	A teacher is off sick and someone covers; two lessons swap; a room is
	unavailable. The pattern stays as designed and only that date moves, which
	is both what a school means and what makes cover reportable afterwards —
	who stood in, for whom, and how often.
	"""

	def validate(self):
		self.copy_lesson_context()
		self.validate_change()

	def copy_lesson_context(self):
		"""Snapshot what the lesson was, so the record reads on its own."""
		lesson = frappe.db.get_value(
			"Course Schedule",
			self.course_schedule,
			["student_group", "course", "instructor", "room", "schedule_date"],
			as_dict=True,
		)
		if not lesson:
			frappe.throw(_("Lesson {0} not found").format(self.course_schedule))

		self.student_group = lesson.student_group
		self.course = lesson.course
		self.schedule_date = self.schedule_date or lesson.schedule_date
		# Only captured the first time; afterwards it is history.
		if not self.original_instructor:
			self.original_instructor = lesson.instructor
		if not self.original_room:
			self.original_room = lesson.room

	def validate_change(self):
		if self.change_type == "Substitute":
			if not self.instructor:
				frappe.throw(_("Choose the instructor who is covering."))
			if self.instructor == self.original_instructor:
				frappe.throw(_("The covering instructor is the same as the original."))

		if self.change_type == "Room Change":
			if not self.room:
				frappe.throw(_("Choose the new room."))
			if self.room == self.original_room:
				frappe.throw(_("The new room is the same as the original."))

	def on_submit(self):
		self.apply_to_lesson()

	def apply_to_lesson(self):
		"""Write the change onto the dated lesson itself.

		Done with db_set rather than a full save: Education re-validates
		overlaps on save, and a substitution has already been checked against
		the conflict engine by the endpoint that created it.
		"""
		if self.change_type == "Cancelled":
			frappe.db.set_value(
				"Course Schedule", self.course_schedule, "docstatus", 2, update_modified=False
			)
			return

		if self.instructor:
			frappe.db.set_value(
				"Course Schedule", self.course_schedule, "instructor", self.instructor
			)
		if self.room:
			frappe.db.set_value("Course Schedule", self.course_schedule, "room", self.room)
