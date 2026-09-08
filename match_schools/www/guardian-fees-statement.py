import frappe
from frappe import _


def get_context(context):
	# Staff-only page; guardian is chosen via the search box and data is
	# loaded over the whitelisted API.
	if frappe.session.user == "Guest":
		frappe.throw(_("Please login to view this page."), frappe.PermissionError)

	context.no_cache = 1
	context.show_sidebar = False
	context.title = _("Guardian Fees Statement")

	# Resolve defaults here (full Python) instead of in the Jinja sandbox.
	context.default_currency = frappe.db.get_default("currency") or ""
	context.default_company = (
		frappe.defaults.get_user_default("company")
		or frappe.db.get_default("company")
		or ""
	)
	return context
