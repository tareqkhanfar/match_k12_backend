"""The Student Applicant workflow.

Education ships `application_status` as a plain Select, so nothing stops a
clerk from jumping an applicant straight to Admitted or reversing a rejection.
A Workflow turns those values into states with explicit transitions and the
role allowed to make each one:

    Applied ──approve──▶ Approved ──admit──▶ Admitted
        │                    │
        └──reject──▶ Rejected ◀──reject──┘

Admitted is terminal: it means a Student record and a login now exist.
"""

import frappe

WORKFLOW_NAME = "Student Admission Workflow"
STATE_FIELD = "application_status"

STATES = [
	# state, doc_status, style, the role that may edit in this state
	("Applied", 0, "Warning", "MS Secretary"),
	("Approved", 0, "Success", "MS School Admin"),
	("Rejected", 0, "Danger", "MS School Admin"),
	("Admitted", 0, "Primary", "MS School Admin"),
]

TRANSITIONS = [
	# state, action, next_state, allowed role
	("Applied", "قبول", "Approved", "MS School Admin"),
	("Applied", "رفض", "Rejected", "MS School Admin"),
	("Approved", "تسجيل", "Admitted", "MS School Admin"),
	("Approved", "رفض", "Rejected", "MS School Admin"),
	("Rejected", "إعادة فتح", "Applied", "MS School Admin"),
]


def create_workflow():
	"""Idempotent: rebuilds the states and transitions in place."""
	_ensure_states()

	doc = (
		frappe.get_doc("Workflow", WORKFLOW_NAME)
		if frappe.db.exists("Workflow", WORKFLOW_NAME)
		else frappe.new_doc("Workflow")
	)
	doc.workflow_name = WORKFLOW_NAME
	doc.document_type = "Student Applicant"
	doc.workflow_state_field = STATE_FIELD
	doc.is_active = 1
	doc.send_email_alert = 0

	doc.set("states", [])
	for state, doc_status, style, role in STATES:
		doc.append(
			"states",
			{
				"state": state,
				"doc_status": doc_status,
				"allow_edit": role,
				"style": style,
			},
		)

	doc.set("transitions", [])
	for state, action, next_state, role in TRANSITIONS:
		doc.append(
			"transitions",
			{
				"state": state,
				"action": action,
				"next_state": next_state,
				"allowed": role,
				"allow_self_approval": 1,
			},
		)

	doc.save(ignore_permissions=True)
	return doc.name


def _ensure_states():
	"""Workflow State rows the workflow refers to."""
	for state, _ds, style, _role in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state, "style": style}
			).insert(ignore_permissions=True)

	for _state, action, _next, _role in TRANSITIONS:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)
