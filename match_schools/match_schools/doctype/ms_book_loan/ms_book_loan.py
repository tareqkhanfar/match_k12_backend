# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, today


class MSBookLoan(Document):
	def validate(self):
		self.set_defaults()
		self.validate_dates()
		self.validate_availability()
		self.flag_overdue()

	def set_defaults(self):
		if not self.issue_date:
			self.issue_date = today()
		if not self.due_date:
			self.due_date = add_days(self.issue_date, 14)
		if not self.issued_by:
			self.issued_by = frappe.session.user

	def validate_dates(self):
		if getdate(self.due_date) < getdate(self.issue_date):
			frappe.throw(_("Due date cannot be before the issue date."))
		if self.return_date and getdate(self.return_date) < getdate(self.issue_date):
			frappe.throw(_("Return date cannot be before the issue date."))
		if self.status == "Returned" and not self.return_date:
			self.return_date = today()

	def validate_availability(self):
		"""Block issuing a book that has no free copy left."""
		if self.status not in ("Issued", "Overdue"):
			return
		total = frappe.db.get_value("MS Library Book", self.book, "total_copies") or 0
		on_loan = frappe.db.count(
			"MS Book Loan",
			{
				"book": self.book,
				"status": ["in", ["Issued", "Overdue"]],
				"name": ["!=", self.name],
			},
		)
		if on_loan >= total:
			frappe.throw(_("No copies of this book are currently available."))

	def flag_overdue(self):
		if self.status == "Issued" and self.due_date and getdate(self.due_date) < getdate(today()):
			self.status = "Overdue"

	def on_update(self):
		self.refresh_book_stock()

	def after_delete(self):
		self.refresh_book_stock()

	def refresh_book_stock(self):
		"""Keep the book's available_copies in step with its loans."""
		if not self.book:
			return
		book = frappe.get_doc("MS Library Book", self.book)
		book.sync_available_copies()
		book.db_set("available_copies", book.available_copies, update_modified=False)
