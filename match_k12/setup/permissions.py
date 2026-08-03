# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Grant the K12 roles doctype-level permissions.

The API layer decides *what* each persona may do, but Frappe still enforces
its own document permissions underneath. Without these rows every write is
rejected with a PermissionError, even for an endpoint the persona is allowed
to call. Administrator never hit this because it bypasses permission checks.
"""

import frappe

from match_k12.api.utils import FRAPPE_ROLE_BY_PERSONA

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
	"Guardian": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, PARENT: READ},
	"Student Guardian": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, PARENT: READ},
	"Instructor": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Student Log": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: WRITE},
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
	"Room": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ},
	# --- Enrolment --------------------------------------------------------
	"Program Enrollment": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ, STUDENT: READ, PARENT: READ},
	"Program Enrollment Course": {ADMIN: FULL, SECRETARY: FULL, TEACHER: READ},
	# --- Daily ------------------------------------------------------------
	"Student Attendance": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Course Schedule": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: REPORT, STUDENT: READ, PARENT: READ},
	# --- Assessment -------------------------------------------------------
	"Assessment Plan": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Plan Criteria": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Result": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Result Detail": {ADMIN: FULL, SECRETARY: FULL, TEACHER: FULL, STUDENT: READ, PARENT: READ},
	"Assessment Criteria": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: WRITE},
	"Assessment Group": {ADMIN: FULL, SECRETARY: WRITE, TEACHER: READ},
	"Grading Scale": {ADMIN: FULL, SECRETARY: READ, TEACHER: READ},
	"Grading Scale Interval": {ADMIN: FULL, SECRETARY: READ, TEACHER: READ},
	# --- Finance ----------------------------------------------------------
	"Fees": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Fee Component": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Fee Structure": {ADMIN: FULL, SECRETARY: FULL, STUDENT: READ, PARENT: READ},
	"Fee Category": {ADMIN: FULL, SECRETARY: WRITE},
	"Fee Schedule": {ADMIN: FULL, SECRETARY: WRITE},
	# --- Settings ---------------------------------------------------------
	# Education Settings and Company are single/system docs the admin edits
	# through our Settings screen.
	"Education Settings": {ADMIN: WRITE, SECRETARY: READ},
	"Company": {ADMIN: WRITE, SECRETARY: READ, TEACHER: READ, STUDENT: READ, PARENT: READ},
	# Saving a Student makes Education create a linked User and Customer, so
	# whoever manages students needs to be able to create those too.
	"User": {ADMIN: WRITE, SECRETARY: WRITE},
	"Customer": {ADMIN: WRITE, SECRETARY: WRITE},
	"Has Role": {ADMIN: WRITE, SECRETARY: WRITE},
}


def apply_permissions():
	"""Create or refresh the Custom DocPerm rows for every K12 role."""
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
	"""Drop every K12 permission row — used when uninstalling."""
	roles = list(FRAPPE_ROLE_BY_PERSONA.values())
	frappe.db.delete("Custom DocPerm", {"role": ["in", roles]})
	frappe.clear_cache()
