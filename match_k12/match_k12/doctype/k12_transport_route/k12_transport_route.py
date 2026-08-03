# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class K12TransportRoute(Document):
	def validate(self):
		if cint(self.capacity) < 0:
			frappe.throw(_("Capacity cannot be negative."))

	@property
	def assigned_count(self) -> int:
		return frappe.db.count("K12 Transport Assignment", {"route": self.name, "active": 1})
