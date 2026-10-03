# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MSPrintDesign(Document):
	"""The school's own design for one printed page of the system («خطاب
	المتسع», «شهادة انتقال الطالب», a printed table…), named by the page's key
	in `match_schools.api.print_designs.DOCUMENTS`. With no row here the page
	prints with its default; edited and reset from Settings › تصاميم الطباعة."""

	pass
