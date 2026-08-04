# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Bulk actions over a selection of rows.

Every table in the system can select several rows and act on them at once, the
way an ERPNext list view does. Each action is whitelisted per doctype so a
selection can never be turned into an arbitrary write.
"""

import frappe
from frappe import _
from frappe.utils import cint

from match_k12.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_SECRETARY,
	ROLE_TEACHER,
	fail,
	k12_endpoint,
	resolve_scope,
)

# Doctypes a teacher may only touch on their own rows.
OWNED_BY_INSTRUCTOR = {"K12 Assignment": "instructor"}

MAX_BULK = 500

# doctype -> {"delete": [roles], "set": {field: [roles]}}
#
# Deliberately narrow: only the operations a screen actually offers, so a
# crafted request cannot reach a field the UI never exposes.
BULK_RULES: dict[str, dict] = {
	"Student": {
		"delete": [],  # students are disabled, never deleted
		"set": {"enabled": list(BACK_OFFICE)},
	},
	"Guardian": {"delete": list(BACK_OFFICE), "set": {}},
	"K12 Announcement": {
		"delete": list(BACK_OFFICE),
		"set": {"published": list(BACK_OFFICE)},
	},
	"K12 Assignment": {
		"delete": [ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER],
		"set": {"status": [ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER]},
	},
	"K12 Library Book": {"delete": list(BACK_OFFICE), "set": {}},
	"K12 Book Loan": {
		"delete": list(BACK_OFFICE),
		"set": {"status": list(BACK_OFFICE)},
	},
	"K12 Behaviour Record": {"delete": [ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER], "set": {}},
	"K12 Transport Assignment": {
		"delete": list(BACK_OFFICE),
		"set": {"status": list(BACK_OFFICE)},
	},
	"K12 Transport Route": {"delete": list(BACK_OFFICE), "set": {}},
	"Fees": {"delete": [], "set": {}},
}


def _reason(exc: Exception) -> str:
	"""A readable failure reason; Frappe exceptions often stringify to ''."""
	text = str(exc).strip()
	if not text:
		messages = frappe.local.message_log or []
		text = "; ".join(
			(m.get("message") if isinstance(m, dict) else str(m)) for m in messages
		).strip()
	return (text or type(exc).__name__)[:200]


def _names(records: str | list) -> list[str]:
	parsed = frappe.parse_json(records) if isinstance(records, str) else records
	if isinstance(parsed, str):
		parsed = [parsed]
	return [r for r in (parsed or []) if r][:MAX_BULK]


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def bulk_delete(doctype: str, records: str | list, persona: str = None):
	"""Delete several records, skipping any that are still referenced."""
	rule = BULK_RULES.get(doctype)
	if not rule or persona not in rule["delete"]:
		frappe.throw(_("You are not allowed to delete these records."), frappe.PermissionError)

	names = _names(records)
	if not names:
		return fail(message_en="Nothing selected.", message_ar="لم يتم تحديد أي سجل.")

	names = _own_rows_only(doctype, names, persona)

	deleted, failed = [], []
	for name in names:
		try:
			frappe.delete_doc(doctype, name)
			deleted.append(name)
		except Exception as exc:
			# A link to another document is the usual reason, and the caller
			# should see which rows survived rather than the whole call failing.
			failed.append({"name": name, "reason": _reason(exc)})

	frappe.db.commit()
	return {
		"success": True,
		"data": {"deleted": len(deleted), "failed": failed},
		"message_en": f"Deleted {len(deleted)} of {len(names)}.",
		"message_ar": f"تم حذف {len(deleted)} من {len(names)}.",
	}


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def bulk_update(
	doctype: str,
	records: str | list,
	field: str,
	# JSON sends numbers and booleans as-is, so the annotation must accept them
	# or Frappe's type validation rejects the call before it runs.
	value: str | int | float | None = None,
	persona: str = None,
):
	"""Set one field across several records."""
	rule = BULK_RULES.get(doctype)
	allowed_roles = (rule or {}).get("set", {}).get(field)
	if not allowed_roles or persona not in allowed_roles:
		frappe.throw(_("You are not allowed to change this field."), frappe.PermissionError)

	names = _names(records)
	if not names:
		return fail(message_en="Nothing selected.", message_ar="لم يتم تحديد أي سجل.")

	meta = frappe.get_meta(doctype)
	df = meta.get_field(field)
	if not df:
		return fail(message_en="Unknown field.", message_ar="حقل غير معروف.")
	if df.fieldtype == "Check":
		value = cint(value)

	names = _own_rows_only(doctype, names, persona)

	updated, failed = [], []
	for name in names:
		try:
			doc = frappe.get_doc(doctype, name)
			setattr(doc, field, value)
			doc.save()
			updated.append(name)
		except Exception as exc:
			failed.append({"name": name, "reason": _reason(exc)})

	frappe.db.commit()
	return {
		"success": True,
		"data": {"updated": len(updated), "failed": failed},
		"message_en": f"Updated {len(updated)} of {len(names)}.",
		"message_ar": f"تم تحديث {len(updated)} من {len(names)}.",
	}


def _own_rows_only(doctype: str, names: list[str], persona: str) -> list[str]:
	"""A teacher's selection is narrowed to rows they own.

	The UI only ever lists their own records, but the selection arrives from
	the client, so it is filtered again here.
	"""
	field = OWNED_BY_INSTRUCTOR.get(doctype)
	if persona != ROLE_TEACHER or not field:
		return names

	instructor = resolve_scope(persona).get("instructor")
	if not instructor:
		return []
	return frappe.get_all(
		doctype,
		filters={"name": ["in", names], field: instructor},
		pluck="name",
	)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def bulk_options(doctype: str, persona: str = None):
	"""Which bulk actions this persona may run on this doctype."""
	rule = BULK_RULES.get(doctype) or {"delete": [], "set": {}}
	return {
		"can_delete": persona in rule["delete"],
		"fields": [f for f, roles in rule["set"].items() if persona in roles],
	}
