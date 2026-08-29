"""A folder in a teacher's own file store.

The tree is held by `parent_folder` alone. `depth` is stored only so the API
can refuse a folder nested deeper than the breadcrumb can show — it is derived,
never authoritative.
"""

from frappe.model.document import Document


class MSDriveFolder(Document):
	pass
