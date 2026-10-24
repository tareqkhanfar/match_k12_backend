# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSPageBlock(Document):
	"""Pages of the portal closed to a group of people, for a while or until lifted.

	Who: the whole school, grades, sections, subjects, teachers or named users —
	narrowed to students, parents, teachers or staff within that group. What:
	pages of the portal, or all of it. The rules live in `api/page_blocks.py`;
	the principal and system managers are never blocked, and the home, alerts,
	profile and help pages always stay open so a blocked person can read why.
	"""

	def on_update(self):
		from match_schools.api.page_blocks import invalidate

		invalidate()

	def on_trash(self):
		from match_schools.api.page_blocks import invalidate

		invalidate()
