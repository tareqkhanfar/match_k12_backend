"""إشعار الطالب ووليّ أمره بفواتير الرسوم ودفعاتها.

فاتورة رسوم تُعتمد، أو دفعة تُسجَّل عليها، يجب أن تصل الأسرة لحظتها لا حين
يفتح أحدهم شاشة الرسوم مصادفةً. الإرسال يمرّ عبر `push.notify`، أي مهمة
خلفية بعد الـcommit: لا يُبطئ اعتماد الفاتورة، ولا يرنّ الهاتف لفاتورة
تراجعت معاملتها.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, fmt_money

from match_schools import push

# القناة موجودة مسبقاً في التطبيق؛ قناة جديدة تحتاج إصداراً جديداً منه، وبدونها
# يُسقط أندرويد الإشعار إلى قناة «أخرى» الافتراضية.
CHANNEL = "alerts"


def _bulk_write_in_progress() -> bool:
	"""استيراد بيانات أو ترحيل: فواتير تاريخية لا يجب أن ترنّ لها الهواتف."""
	flags = frappe.flags
	return bool(flags.in_import or flags.in_migrate or flags.in_patch or flags.in_install)


def _family_users(student: str) -> list[str]:
	"""حساب الطالب وحسابات أولياء أمره المفعّلة."""
	users: list[str] = []
	account = frappe.db.get_value("Student", student, "user")
	if account:
		users.append(account)

	guardians = frappe.get_all(
		"Student Guardian",
		filters={"parent": student, "parenttype": "Student"},
		pluck="guardian",
	)
	if guardians:
		users += frappe.get_all(
			"Guardian",
			filters={"name": ["in", list(set(guardians))], "user": ["is", "set"]},
			pluck="user",
		)

	users = [u for u in dict.fromkeys(users) if u]
	if not users:
		return []
	# حساب معطّل لا يدخل التطبيق أصلاً.
	return frappe.get_all(
		"User", filters={"name": ["in", users], "enabled": 1}, pluck="name"
	)


def _money(amount, currency: str | None) -> str:
	return fmt_money(flt(amount), currency=currency)


def on_invoice_submit(doc, method=None):
	"""Sales Invoice → on_submit."""
	student = doc.get("student")
	if not student or doc.get("is_return") or _bulk_write_in_progress():
		return

	try:
		users = _family_users(student)
		if not users:
			return
		name = frappe.db.get_value("Student", student, "student_name") or ""
		body = f"المبلغ {_money(doc.grand_total, doc.currency)}"
		if doc.get("due_date"):
			body += f" — تاريخ الاستحقاق {doc.due_date}"
		push.notify(
			users,
			f"فاتورة رسوم جديدة — {name}".strip(" —"),
			body,
			channel=CHANNEL,
			type="fee_invoice",
			id=f"fee-invoice:{doc.name}",
			ref=doc.name,
			student=student,
		)
	except Exception:
		# إشعار فاشل يجب ألّا يمنع اعتماد فاتورة.
		frappe.log_error(title="fee push: invoice", message=frappe.get_traceback())


def _payment_students(doc) -> dict[str, dict]:
	"""الطلاب الذين تخصّهم الدفعة: `{student: {amount, invoice}}`."""
	out: dict[str, dict] = {}

	for ref in doc.get("references") or []:
		if ref.reference_doctype != "Sales Invoice":
			continue
		student = frappe.db.get_value("Sales Invoice", ref.reference_name, "student")
		if not student:
			continue
		entry = out.setdefault(student, {"amount": 0.0, "invoices": []})
		entry["amount"] += flt(ref.allocated_amount)
		entry["invoices"].append(ref.reference_name)

	if out:
		return out

	# دفعة على الحساب بلا فاتورة مرجعية.
	if doc.party_type == "Student" and doc.party:
		return {doc.party: {"amount": flt(doc.paid_amount), "invoices": []}}
	if doc.party_type == "Customer" and doc.party:
		# عميل واحد قد يجمع إخوة؛ الدفعة تخصّ حساب الأسرة كلّه.
		for student in frappe.get_all("Student", filters={"customer": doc.party}, pluck="name"):
			out[student] = {"amount": flt(doc.paid_amount), "invoices": []}
	return out


def on_payment_submit(doc, method=None):
	"""Payment Entry → on_submit."""
	if doc.get("payment_type") != "Receive" or _bulk_write_in_progress():
		return

	try:
		for student, info in _payment_students(doc).items():
			users = _family_users(student)
			if not users:
				continue
			name = frappe.db.get_value("Student", student, "student_name") or ""
			body = f"المبلغ المدفوع {_money(info['amount'], doc.paid_to_account_currency)}"
			if len(info["invoices"]) == 1:
				outstanding = frappe.db.get_value(
					"Sales Invoice", info["invoices"][0], "outstanding_amount"
				)
				body += f" — المتبقي {_money(outstanding, doc.paid_to_account_currency)}"
			push.notify(
				users,
				f"تم تسجيل دفعة رسوم — {name}".strip(" —"),
				body,
				channel=CHANNEL,
				type="fee_payment",
				id=f"fee-payment:{doc.name}:{student}",
				ref=doc.name,
				student=student,
			)
	except Exception:
		frappe.log_error(title="fee push: payment", message=frappe.get_traceback())
