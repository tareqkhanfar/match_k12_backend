
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Rules that keep a school Sales Invoice sound, wherever it is created.

These run as document hooks rather than inside an API endpoint, so they apply
to an invoice typed into the ERPNext desk, imported, created by a script or
raised by this app's own screens. Accounting rules that only hold on one path
are not rules.

What is enforced:

  * an invoice naming a student must name the enrolment it belongs to
  * that enrolment must belong to that same student
  * the enrolment must not be cancelled
  * the customer must be the one linked to the student

Ordinary sales invoices — no student — are left completely alone.
"""

import frappe
from frappe import _


def validate_student_invoice(doc, method=None):
	"""Hooked on Sales Invoice validate."""
	if not doc.get("student"):
		# Not a school invoice. A stray enrolment link would be meaningless,
		# so clear it rather than leave it dangling.
		if doc.get("ms_program_enrollment"):
			doc.ms_program_enrollment = None
		return

	if not frappe.db.exists("Student", doc.student):
		frappe.throw(_("Student {0} does not exist").format(doc.student))

	_require_enrollment(doc)
	_validate_enrollment_belongs_to_student(doc)
	_validate_customer_matches_student(doc)
	_copy_enrollment_context(doc)


def _require_enrollment(doc):
	if doc.get("ms_program_enrollment"):
		return

	# Fill it in when there is exactly one candidate; a school with a single
	# enrolment per student should not have to state the obvious.
	candidates = frappe.get_all(
		"Program Enrollment",
		filters={"student": doc.student, "docstatus": 1},
		pluck="name",
		order_by="creation desc",
	)
	if len(candidates) == 1:
		doc.ms_program_enrollment = candidates[0]
		return

	if not candidates:
		frappe.throw(
			_(
				"Student {0} has no submitted Program Enrollment. "
				"Enrol the student before invoicing."
			).format(frappe.bold(doc.student))
		)

	frappe.throw(
		_("Select the Program Enrollment this invoice belongs to ({0} available).").format(
			len(candidates)
		)
	)


def _validate_enrollment_belongs_to_student(doc):
	row = frappe.db.get_value(
		"Program Enrollment",
		doc.ms_program_enrollment,
		["student", "docstatus"],
		as_dict=True,
	)
	if not row:
		frappe.throw(
			_("Program Enrollment {0} does not exist").format(doc.ms_program_enrollment)
		)

	if row.student != doc.student:
		# The check the old Fees doctype made, and the reason it existed: an
		# invoice billed against another child's enrolment is silently wrong.
		frappe.throw(
			_("Program Enrollment {0} belongs to {1}, not to {2}").format(
				frappe.bold(doc.ms_program_enrollment),
				frappe.bold(row.student),
				frappe.bold(doc.student),
			)
		)

	if row.docstatus == 2:
		frappe.throw(
			_("Program Enrollment {0} is cancelled").format(
				frappe.bold(doc.ms_program_enrollment)
			)
		)


def _validate_customer_matches_student(doc):
	"""The receivable must sit on the student's own customer account."""
	customer = frappe.db.get_value("Student", doc.student, "customer")
	if not customer:
		student = frappe.get_doc("Student", doc.student)
		student.set_missing_customer_details()
		customer = frappe.db.get_value("Student", doc.student, "customer")

	if not customer:
		frappe.throw(
			_("Student {0} has no linked Customer, so no receivable can be booked").format(
				frappe.bold(doc.student)
			)
		)

	if not doc.customer:
		doc.customer = customer
	elif doc.customer != customer:
		frappe.throw(
			_(
				"Customer {0} is not the customer of student {1} ({2}). "
				"The receivable would be booked against the wrong account."
			).format(
				frappe.bold(doc.customer), frappe.bold(doc.student), frappe.bold(customer)
			)
		)


def _copy_enrollment_context(doc):
	"""Denormalise programme/year/term so reports can group without a join."""
	row = frappe.db.get_value(
		"Program Enrollment",
		doc.ms_program_enrollment,
		["program", "academic_year", "academic_term"],
		as_dict=True,
	)
	if not row:
		return
	doc.ms_program = row.program
	doc.ms_academic_year = row.academic_year
	doc.ms_academic_term = row.academic_term
