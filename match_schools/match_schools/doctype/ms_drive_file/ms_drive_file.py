"""One file in a teacher's own store.

The bytes live in Frappe's own File doctype; this row is the drive's view of
them — where the teacher filed it, what they called it, and who they let see
it. Deleting the row deletes the File with it, which is why `file_url` is
never shared between two rows.
"""

from frappe.model.document import Document


class MSDriveFile(Document):
	pass
