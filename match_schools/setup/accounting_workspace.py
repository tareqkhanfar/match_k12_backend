# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Attach the match_utils accounting tools to the Education Accounting screens.

`match_utils` owns the parts of a school's accounting that ERPNext does not
ship: المصروفات (`Expense`), its `Expense Type` chart, and the customer and
supplier statements. Every Match site installs it, so those belong on the
Education Accounting workspace beside the fee reports.

They cannot simply be listed in `education_accounting.json`, though. A
Workspace Link is a real link field: Frappe validates it on save and refuses
the document with `LinkValidationError: Could not find ...` when the target
doctype is absent. Shipping the rows in the file would therefore make
`bench migrate` **fail outright** on any site without match_utils — including
a fresh bench, and this app's own test bench.

So the file carries only what frappe/erpnext/education guarantee, and these
rows are appended here on install and on every migrate, guarded on the target
actually existing. A site with match_utils gets the full accounting program; a
site without it still migrates, just without that card.

Idempotent: a link already present is never added twice, so running on every
migrate does not grow the workspace.
"""

import frappe

WORKSPACE = "Education Accounting"
SIDEBAR = "Education Accounting"

CARD_LABEL = "المصروفات وكشوف الحسابات (match_utils)"

#: (label, link_type, link_to, is_query_report, dependencies)
MATCH_UTILS_LINKS = [
	("المصروفات", "DocType", "Expense", 0, ""),
	("أنواع المصروفات", "DocType", "Expense Type", 0, ""),
	("كشف حساب عميل", "Report", "Customer Statement of Account", 1, "GL Entry"),
	("كشف حساب مورّد", "Report", "Supplier Statement of Account", 1, "GL Entry"),
]

#: The same tools in the left-hand nav, under their own section.
SIDEBAR_SECTION = "المصروفات وكشوف الحسابات"


def sync():
	"""Called from `setup.install.after_install`, which also runs on migrate."""
	available = [row for row in MATCH_UTILS_LINKS if _exists(row)]
	if not available:
		# match_utils is not on this site. Leave both documents alone — and
		# remove anything a previous run added, so uninstalling match_utils
		# does not leave dead links behind that block the next save.
		_remove_from_workspace()
		_remove_from_sidebar()
		return

	_add_to_workspace(available)
	_add_to_sidebar(available)


def _exists(row):
	_label, link_type, link_to, _is_qr, _deps = row
	return bool(frappe.db.exists(link_type, link_to))


# --- Workspace -------------------------------------------------------------


def _add_to_workspace(available):
	if not frappe.db.exists("Workspace", WORKSPACE):
		return

	doc = frappe.get_doc("Workspace", WORKSPACE)
	present = {l.link_to for l in doc.links if l.type == "Link"}
	wanted = [r for r in available if r[2] not in present]

	has_card = any(l.type == "Card Break" and l.label == CARD_LABEL for l in doc.links)
	if not wanted and has_card:
		return

	if not has_card:
		doc.append("links", {
			"type": "Card Break", "label": CARD_LABEL, "link_type": "DocType",
			"link_to": None, "link_count": len(available), "hidden": 0,
			"onboard": 0, "is_query_report": 0,
		})

	for label, link_type, link_to, is_qr, deps in wanted:
		doc.append("links", {
			"type": "Link", "label": label, "link_type": link_type, "link_to": link_to,
			"is_query_report": is_qr, "dependencies": deps, "link_count": 0,
			"hidden": 0, "onboard": 0,
		})

	_set_card_count(doc, len(available))
	doc.content = _content_with_card(doc.content)
	doc.flags.ignore_permissions = True
	doc.save()


def _set_card_count(doc, count):
	for link in doc.links:
		if link.type == "Card Break" and link.label == CARD_LABEL:
			link.link_count = count


def _content_with_card(content):
	"""Put the card on the page as well as in the links table.

	A Card Break with no matching block in `content` is stored but never
	rendered — the workspace body is laid out from `content` alone.
	"""
	import hashlib
	import json

	blocks = json.loads(content or "[]")
	if any(b.get("type") == "card" and b.get("data", {}).get("card_name") == CARD_LABEL
	       for b in blocks):
		return content

	blocks.append({
		"id": hashlib.md5(("card-" + CARD_LABEL).encode()).hexdigest()[:10],
		"type": "card",
		"data": {"card_name": CARD_LABEL, "col": 4},
	})
	return json.dumps(blocks, ensure_ascii=False)


def _remove_from_workspace():
	if not frappe.db.exists("Workspace", WORKSPACE):
		return

	doc = frappe.get_doc("Workspace", WORKSPACE)
	ours = {row[2] for row in MATCH_UTILS_LINKS}
	keep = [
		l for l in doc.links
		if not (l.type == "Card Break" and l.label == CARD_LABEL)
		and not (l.type == "Link" and l.link_to in ours)
	]
	if len(keep) == len(doc.links):
		return

	doc.set("links", [])
	for l in keep:
		doc.append("links", l.as_dict())
	doc.content = _content_without_card(doc.content)
	doc.flags.ignore_permissions = True
	doc.save()


def _content_without_card(content):
	import json

	blocks = json.loads(content or "[]")
	kept = [
		b for b in blocks
		if not (b.get("type") == "card" and b.get("data", {}).get("card_name") == CARD_LABEL)
	]
	return json.dumps(kept, ensure_ascii=False)


# --- Sidebar ---------------------------------------------------------------


def _add_to_sidebar(available):
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return

	doc = frappe.get_doc("Workspace Sidebar", SIDEBAR)
	present = {i.link_to for i in doc.items if i.type == "Link"}
	wanted = [r for r in available if r[2] not in present]
	has_section = any(i.type == "Section Break" and i.label == SIDEBAR_SECTION for i in doc.items)

	if not wanted and has_section:
		return

	if not has_section:
		doc.append("items", {
			"type": "Section Break", "label": SIDEBAR_SECTION, "link_type": "DocType",
			"icon": "ri-money-dollar-circle-line", "child": 0, "indent": 1,
			"collapsible": 1, "keep_closed": 1, "show_arrow": 0,
		})

	for label, link_type, link_to, _is_qr, _deps in wanted:
		doc.append("items", {
			"type": "Link", "label": label, "link_type": link_type, "link_to": link_to,
			"child": 1, "indent": 0, "collapsible": 1, "keep_closed": 0, "show_arrow": 0,
		})

	doc.flags.ignore_permissions = True
	# `export_sidebar` rewrites the app's JSON file from the saved document when
	# developer mode is on. That would bake the match_utils rows into the file
	# and reintroduce the migrate failure this module exists to prevent.
	doc.flags.ignore_export = True
	_save_sidebar_without_export(doc)


def _remove_from_sidebar():
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return

	doc = frappe.get_doc("Workspace Sidebar", SIDEBAR)
	ours = {row[2] for row in MATCH_UTILS_LINKS}
	keep = [
		i for i in doc.items
		if not (i.type == "Section Break" and i.label == SIDEBAR_SECTION)
		and not (i.type == "Link" and i.link_to in ours)
	]
	if len(keep) == len(doc.items):
		return

	doc.set("items", [])
	for i in keep:
		doc.append("items", i.as_dict())
	doc.flags.ignore_permissions = True
	_save_sidebar_without_export(doc)


def _save_sidebar_without_export(doc):
	"""Save without letting the controller rewrite the shipped JSON file."""
	original = frappe.conf.get("developer_mode")
	try:
		frappe.conf["developer_mode"] = 0
		doc.save()
	finally:
		if original is None:
			frappe.conf.pop("developer_mode", None)
		else:
			frappe.conf["developer_mode"] = original
