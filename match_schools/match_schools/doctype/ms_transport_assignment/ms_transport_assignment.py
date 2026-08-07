# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate


class MSTransportAssignment(Document):
	def validate(self):
		self.validate_dates()
		self.validate_single_active_route()
		self.validate_capacity()

	def validate_dates(self):
		if self.start_date and self.end_date and getdate(self.end_date) < getdate(self.start_date):
			frappe.throw(_("End date cannot be before the start date."))

	def validate_single_active_route(self):
		"""A student may only ride one active route at a time."""
		if not self.active:
			return
		existing = frappe.db.exists(
			"MS Transport Assignment",
			{"student": self.student, "active": 1, "name": ["!=", self.name]},
		)
		if existing:
			frappe.throw(_("This student is already assigned to an active route."))

	def validate_capacity(self):
		if not self.active or not self.route:
			return
		capacity = cint(frappe.db.get_value("MS Transport Route", self.route, "capacity"))
		if not capacity:
			return
		assigned = frappe.db.count(
			"MS Transport Assignment",
			{"route": self.route, "active": 1, "name": ["!=", self.name]},
		)
		if assigned >= capacity:
			frappe.throw(_("This route is already at full capacity ({0}).").format(capacity))
