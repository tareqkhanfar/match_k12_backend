# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class MSGradeScheme(Document):
	def validate(self):
		self.validate_weights()
		self.validate_single_default()

	def validate_weights(self):
		"""Weights must add up to 100% — per quarter when the plan uses them.

		A plan is now a tree: a category carries the weight, and the individual
		assessments inside it carry none (the category's weight is what reaches
		the final mark). Only categories are summed, and a term split into two
		quarters is checked as two separate hundreds rather than one two
		hundred.
		"""
		if not self.components:
			frappe.throw(_("Add at least one component."))

		# Assessments nested inside a category contribute no weight of their own.
		categories = [
			c
			for c in self.components
			if c.component_type != "Bonus" and not c.get("ms_parent_component")
		]
		if not categories:
			frappe.throw(_("A scheme needs at least one non-bonus component."))

		self.total_weight = sum(flt(c.weight) for c in categories)

		by_quarter: dict[str, float] = {}
		for c in categories:
			by_quarter.setdefault(c.get("ms_quarter") or "", 0.0)
			by_quarter[c.get("ms_quarter") or ""] += flt(c.weight)

		# A quarter's categories are written in that quarter's own marks — 40
		# for a 40-mark quarter — so the expected total comes from the term's
		# quarter definition. A plan with no quarters (the flat plans that
		# predate them) is still checked against 100.
		expected_by_quarter = self._quarter_totals()

		for quarter, total in by_quarter.items():
			expected = expected_by_quarter.get(
				quarter, (flt(self.get("ms_term_total")) or 100) if not quarter else None
			)
			if expected is None:
				# The quarter no longer exists on the term; the API reports this
				# more clearly, so the doctype does not block the save here.
				continue
			# Allow a rounding cent either way.
			if abs(total - expected) > 0.01:
				label = quarter or _("the plan")
				frappe.throw(
					_("Weights for {0} must total {1}. They currently total {2}.").format(
						label, round(expected, 2), round(total, 2)
					)
				)

	def _quarter_totals(self) -> dict:
		"""How many marks each quarter of this plan's term is worth."""
		if not self.academic_term:
			return {}
		rows = frappe.get_all(
			"MS Term Quarter",
			filters={"parent": self.academic_term, "parenttype": "Academic Term"},
			fields=["quarter_name", "total_marks"],
			order_by="idx",
		)
		# A subject marked out of 200 has the term's quarters as shares of 200
		# (80 and 120 for a 40/60 term), not the term's own 40 and 60.
		from match_schools.api.assessment_plan import scale_quarters

		scaled = scale_quarters(
			[{"name": r.quarter_name, "totalMarks": flt(r.total_marks)} for r in rows],
			flt(self.get("ms_term_total")),
		)
		return {q["name"]: flt(q["totalMarks"]) for q in scaled}

		for c in self.components:
			if flt(c.max_score) <= 0:
				frappe.throw(_("Max score must be greater than zero for {0}.").format(c.component_name))

	def validate_single_default(self):
		"""Only one default scheme per course/program combination."""
		if not self.is_default:
			return
		clash = frappe.db.exists(
			"MS Grade Scheme",
			{
				"is_default": 1,
				"course": self.course or "",
				"program": self.program or "",
				"name": ["!=", self.name],
			},
		)
		if clash:
			frappe.throw(_("Another default scheme already exists for this course and program."))
