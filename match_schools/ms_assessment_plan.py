# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Assessment Plan, without Education's clash with the timetable.

Education refuses an exam whose time overlaps any Course Schedule of the same
section, teacher or room. In a school day every period is a lesson, so an exam
in period 1 always "conflicts" with the very lesson it replaces — and no exam
could be scheduled at all. Match Schools runs its own conflict check before
saving (`exams.check_conflicts`), shows each clash and lets the office go on
anyway. What stays forbidden here is what can never be right: two exams for
the same section, or in the same room, at the same time.
"""

from education.education.doctype.assessment_plan.assessment_plan import AssessmentPlan


class MSAssessmentPlan(AssessmentPlan):
	def validate_overlap(self):
		from education.education.utils import validate_overlap_for

		if self.student_group:
			validate_overlap_for(self, "Assessment Plan", "student_group")
		validate_overlap_for(self, "Assessment Plan", "room")
