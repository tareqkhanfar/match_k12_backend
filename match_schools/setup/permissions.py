# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Grant the Match Schools roles doctype-level permissions.

The API layer decides *what* each persona may do, but Frappe still enforces
its own document permissions underneath. Without these rows every write is
rejected with a PermissionError, even for an endpoint the persona is allowed
to call. Administrator never hit this because it bypasses permission checks.
"""

import frappe

from match_schools.api.utils import FRAPPE_ROLE_BY_PERSONA

ADMIN = FRAPPE_ROLE_BY_PERSONA["admin"]
SECRETARY = FRAPPE_ROLE_BY_PERSONA["secretary"]
TEACHER = FRAPPE_ROLE_BY_PERSONA["teacher"]
STUDENT = FRAPPE_ROLE_BY_PERSONA["student"]
PARENT = FRAPPE_ROLE_BY_PERSONA["parent"]

# Permission levels, from least to most.
READ = {"read": 1}
REPORT = {"read": 1, "report": 1, "export": 1, "print": 1}
WRITE = {**REPORT, "write": 1, "create": 1}
FULL = {**WRITE, "delete": 1, "submit": 1, "cancel": 1, "amend": 1, "share": 1, "email": 1}

# doctype -> {role: permission level}
#
# Reads are broad because the API scopes every query to the caller anyway
# (a teacher's list only ever contains their own groups). Writes are narrow
# and follow what each persona's endpoints actually need.
MATRIX: dict[str, dict[str, dict]] = {
	# --- Core records -----------------------------------------------------
	"Student": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: REPORT, STUDENT: READ, PARENT: READ},
	"Guardian": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, PARENT: READ},
	"Student Guardian": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, PARENT: READ},
	"Instructor": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Student Log": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: WRITE},
	# --- Admissions -------------------------------------------------------
	# The secretary takes applications; only the admin approves, rejects or
	# admits, which the workflow enforces separately from these row rights.
	"Student Applicant": {ADMIN: FULL, SECRETARY: WRITE},
	"Student Admission": {ADMIN: FULL, SECRETARY: READ},
	"Student Admission Program": {ADMIN: FULL, SECRETARY: READ},
	"Student Sibling": {ADMIN: FULL, SECRETARY: WRITE},
	# --- Structure --------------------------------------------------------
	"Program": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Program Course": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Course": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: REPORT, STUDENT: READ, PARENT: READ},
	"Student Group": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: REPORT, STUDENT: READ, PARENT: READ},
	"Student Group Student": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Student Group Instructor": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ},
	"Student Batch Name": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Student Category": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ},
	"Academic Year": {ADMIN: FULL, SECRETARY: REPORT, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Academic Term": {ADMIN: FULL, SECRETARY: REPORT, TEACHER: READ, STUDENT: READ, PARENT: READ},
	# --- Enrolment --------------------------------------------------------
	"Program Enrollment": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Program Enrollment Course": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ},
	"Program Enrollment Fee": {ADMIN: FULL, SECRETARY: FULL},
	# Submitting a Program Enrollment generates these, so the persona doing the
	# enrolling needs to be able to write them.
	"Course Enrollment": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	# --- Daily ------------------------------------------------------------
	"Student Attendance": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Course Schedule": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: REPORT, STUDENT: READ, PARENT: READ},
	# The weekly pattern the school designs, and the one-day departures from it.
	# A teacher may read both — their own timetable and any cover they are
	# given — but only the office edits them.
	"MS Timetable Slot": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"MS Lesson Change": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	# --- Assessment -------------------------------------------------------
	"Assessment Plan": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Plan Criteria": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Result": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Result Detail": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Criteria": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: WRITE},
	# Teachers schedule exams for their own classes, which creates the
	# Assessment Group behind the exam type on first use.
	"Room": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Assessment Group": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: WRITE},
	"Grading Scale": {ADMIN: FULL, SECRETARY: READ, TEACHER: READ},
	"Grading Scale Interval": {ADMIN: FULL, SECRETARY: READ, TEACHER: READ},
	# --- Finance ----------------------------------------------------------
	"Fees": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Fee Component": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Fee Structure": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Fee Category": {ADMIN: FULL, SECRETARY: WRITE},
	# v16 bills school fees as Sales Invoices against the student's Customer.
	# A family may read its own; the API scopes the query to them.
	"Sales Invoice": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Sales Invoice Item": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Sales Taxes and Charges": {ADMIN: FULL, SECRETARY: FULL},
	"Item": {ADMIN: WRITE, SECRETARY: READ},
	# Recording a payment books a Payment Entry against the receivable account,
	# so whoever takes money needs the accounting documents too.
	"Journal Entry": {ADMIN: FULL, SECRETARY: FULL},
	"Journal Entry Account": {ADMIN: FULL, SECRETARY: FULL},
	"Payment Entry": {ADMIN: FULL, SECRETARY: FULL},
	"Payment Entry Reference": {ADMIN: FULL, SECRETARY: FULL},
	"Mode of Payment": {ADMIN: WRITE, SECRETARY: READ},
	"Mode of Payment Account": {ADMIN: WRITE, SECRETARY: READ},
	"Account": {ADMIN: READ, SECRETARY: READ},
	"Cost Center": {ADMIN: READ, SECRETARY: READ},
	"GL Entry": {ADMIN: READ, SECRETARY: READ},
	"Fee Schedule": {ADMIN: FULL, SECRETARY: WRITE},
	# --- Settings ---------------------------------------------------------
	# Education Settings and Company are single/system docs the admin edits
	# through our Settings screen.
	"Education Settings": {ADMIN: WRITE, SECRETARY: READ},
	"Company": {ADMIN: WRITE, SECRETARY: READ, TEACHER: READ, STUDENT: READ, PARENT: READ},
	# --- Gradebook --------------------------------------------------------
	"MS Grade Scheme": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ},
	"MS Grade Scheme Component": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ},
	"MS Gradebook Entry": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	# The teacher submits; only the back office reviews and publishes.
	"MS Survey": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	# --- Specialist files: nursing, counselling, special needs, and the rest.
	# The design is the back office's; filling one in is a teacher's work too.
	"MS Form Template": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ},
	"MS Form Entry": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE},
	"MS Survey Question": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"MS Survey Response": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: WRITE,
	},
	"MS Survey Answer": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: WRITE,
	},
	"MS Resource": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ},
	"MS Alert Rule": {ADMIN: FULL, SECRETARY: FULL},
	"MS Alert Action": {ADMIN: FULL, SECRETARY: FULL},
	"MS Student Alert": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: WRITE, PARENT: WRITE,
	},
	"MS Quiz": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ},
	"MS Quiz Question": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ},
	"MS Quiz Attempt": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: READ},
	"MS Quiz Answer": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: READ},
	"MS Teacher Observation": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE},
	"MS Observation Criterion": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE},
	"MS Activity": {ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ},
	"MS Activity Enrolment": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: WRITE,
	},
	"MS Timetable Plan": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ},
	"MS Timetable Period": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ},
	"MS Subject Load": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ},
	"MS Term Submission": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ,
	},
	# --- Communication and assignments ------------------------------------
	# These doctypes ship permissions in their own JSON but were missing the
	# secretary, so every back-office write against them was rejected.
	"MS Announcement": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ,
	},
	"MS Message": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: WRITE,
	},
	"MS Assignment": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ,
	},
	"MS Assignment Submission": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: WRITE, PARENT: READ,
	},
	"MS Health Record": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"MS Health Visit": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"MS Behaviour Record": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: WRITE, STUDENT: READ, PARENT: READ,
	},
	"MS Library Book": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"MS Book Loan": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"MS Transport Route": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ,
	},
	"MS Transport Assignment": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ,
	},
	# --- Assignments and attachments --------------------------------------
	# Students upload work, so they need to write the child table and File.
	"MS Attachment": {
		ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: WRITE, PARENT: READ,
	},
	"File": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: WRITE, PARENT: READ},
	# Saving a Student makes Education create a linked User and Customer, so
	# whoever manages students needs to be able to create those too.
	"User": {ADMIN: WRITE, SECRETARY: WRITE},
	"Customer": {ADMIN: WRITE, SECRETARY: WRITE},
	"Has Role": {ADMIN: WRITE, SECRETARY: WRITE},
}


def apply_permissions():
	"""Create or refresh the Custom DocPerm rows for every Match Schools role."""
	applied, skipped = 0, []

	for doctype, roles in MATRIX.items():
		if not frappe.db.exists("DocType", doctype):
			# Education may not ship every doctype in all versions.
			skipped.append(doctype)
			continue

		for role, level in roles.items():
			_set_perm(doctype, role, level)
			applied += 1

	frappe.clear_cache()
	return {"applied": applied, "skipped": skipped}


def _set_perm(doctype: str, role: str, level: dict):
	"""Add a permission row, or top up an existing one."""
	meta = frappe.get_meta(doctype)
	# Submittable doctypes are the only ones where submit/cancel/amend apply.
	perm = dict(level)
	if not meta.is_submittable:
		for key in ("submit", "cancel", "amend"):
			perm.pop(key, None)

	existing = frappe.db.get_value(
		"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}, "name"
	)
	if existing:
		doc = frappe.get_doc("Custom DocPerm", existing)
	else:
		doc = frappe.new_doc("Custom DocPerm")
		doc.parent = doctype
		doc.parenttype = "DocType"
		doc.parentfield = "permissions"
		doc.role = role
		doc.permlevel = 0

	for key, value in perm.items():
		setattr(doc, key, value)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)


def remove_permissions():
	"""Drop every Match Schools permission row — used when uninstalling."""
	roles = list(FRAPPE_ROLE_BY_PERSONA.values())
	frappe.db.delete("Custom DocPerm", {"role": ["in", roles]})
	frappe.clear_cache()
