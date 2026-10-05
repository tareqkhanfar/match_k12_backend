# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSWorkspaceAssignment(Document):
	"""Which workspaces one person may switch between, which one they land on,
	and whether the role's own menu stays among them. No record means the
	role's menu alone, as before workspaces existed."""

	pass
