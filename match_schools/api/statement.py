"""كشف حساب الطالب — من دفتر الأستاذ، لا من قائمة الفواتير.

لكل طالب زبون يُفوتَر باسمه، وحساب ذلك الزبون هو الحقيقة المحاسبية: فيه
رسوم المدرسة، وما اشتُري من مقصفها أو متجرها باسمه، والمرتجعات، والدفعات —
المخصّصة لفواتير وغير المخصّصة — وقيود اليومية. قائمة الفواتير وحدها تُسقط
نصف ذلك: فاتورة متجر لا تحمل حقل الطالب، ودفعة مقدّمة لا تُخصَّص لفاتورة،
فيظهر رصيد يخالف الدفاتر.

لذلك يُبنى الكشف من `GL Entry` لحساب الزبون: الرصيد الجاري والرصيد الختامي
هما ما تقوله الدفاتر حرفياً. ثم يُفصَّل كل مستند: الفاتورة بأصنافها وكمياتها
وأسعارها وخصوماتها وضرائبها وأقساطها، والدفعة بطريقتها ومرجعها وما خُصّص منها
لكل فاتورة وما بقي رصيداً دائناً.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ms_endpoint,
	resolve_scope,
)

OTHER = "other"
OTHER_LABEL = "مشتريات ومعاملات أخرى"


def _r(value) -> float:
	return round(flt(value), 2)


# --- Who may see what --------------------------------------------------------


def students_for(persona: str, scope: dict, student: str | None) -> list[str]:
	"""The children whose account this caller may see.

	A guardian sees every child of theirs and nobody else's; a student sees
	themselves; the office sees the one student it asks about. An empty scope
	stays empty — it never widens into "everyone".
	"""
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		allowed = list(scope.get("students") or [])
		if student and student not in allowed:
			frappe.throw(_("You are not allowed to view these fees."), frappe.PermissionError)
		return allowed
	if not student:
		frappe.throw(_("اختر طالباً لعرض كشف حسابه."))
	if not frappe.db.exists("Student", student):
		frappe.throw(_("الطالب غير موجود."))
	return [student]


# --- The account -------------------------------------------------------------


def _vouchers(customer: str) -> list[dict]:
	"""Every posted movement on the customer's account, one row per document.

	A document may touch the account more than once — a payment split between
	an invoice and an advance, an invoice paid at the counter — so its lines
	are summed, but debit and credit are kept apart: an invoice paid at the
	point of sale is both a charge and a payment, and netting them to zero
	would hide both.
	"""
	rows = frappe.db.sql(
		"""
		SELECT voucher_type, voucher_no,
		       MIN(posting_date) AS posting_date,
		       MIN(creation)     AS creation,
		       SUM(debit)        AS debit,
		       SUM(credit)       AS credit
		  FROM `tabGL Entry`
		 WHERE party_type = 'Customer' AND party = %(customer)s AND is_cancelled = 0
		 GROUP BY voucher_type, voucher_no
		 ORDER BY MIN(posting_date), MIN(creation)
		""",
		{"customer": customer},
		as_dict=True,
	)
	return rows


def _invoices(names: list[str]) -> dict[str, dict]:
	"""Sales invoices with everything a family needs to understand them."""
	if not names:
		return {}
	docs = {
		d.name: d
		for d in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", names]},
			fields=[
				"name", "posting_date", "due_date", "is_return", "return_against",
				"is_opening", "is_pos", "total", "net_total", "discount_amount",
				"additional_discount_percentage", "grand_total", "rounded_total",
				"rounding_adjustment", "disable_rounded_total", "outstanding_amount",
				"paid_amount", "status", "student", "customer", "remarks",
				"ms_academic_year", "ms_academic_term", "ms_program", "currency",
			],
			limit_page_length=0,
		)
	}
	items: dict[str, list] = {}
	for it in frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", names]},
		fields=[
			"parent", "idx", "item_code", "item_name", "qty", "uom",
			"price_list_rate", "discount_percentage", "discount_amount", "rate", "amount",
		],
		order_by="parent, idx",
		limit_page_length=0,
	):
		items.setdefault(it.parent, []).append(it)
	taxes: dict[str, list] = {}
	for t in frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": ["in", names], "parenttype": "Sales Invoice"},
		fields=["parent", "description", "tax_amount_after_discount_amount", "tax_amount"],
		order_by="parent, idx",
		limit_page_length=0,
	):
		taxes.setdefault(t.parent, []).append(t)
	schedule: dict[str, list] = {}
	for s in frappe.get_all(
		"Payment Schedule",
		filters={"parent": ["in", names], "parenttype": "Sales Invoice"},
		fields=["parent", "idx", "due_date", "payment_amount", "description"],
		order_by="parent, due_date, idx",
		limit_page_length=0,
	):
		schedule.setdefault(s.parent, []).append(s)

	out = {}
	now = getdate(today())
	for name, d in docs.items():
		receivable = flt(d.grand_total) if cint(d.disable_rounded_total) or not flt(d.rounded_total) else flt(d.rounded_total)
		lines = []
		item_discount = 0.0
		for it in items.get(name, []):
			qty = flt(it.qty)
			rate = flt(it.rate)
			list_rate = flt(it.price_list_rate)
			# A discount is what the system recorded as one — a price above the
			# list price is a different price, not a negative discount.
			recorded = flt(it.discount_percentage) > 0 or flt(it.discount_amount) > 0
			per_unit = (list_rate - rate) if recorded and list_rate > rate else 0.0
			line_discount = abs(per_unit * qty)
			# A negative line on an ordinary invoice is a discount written as an
			# item ("خصم إخوة"), and reads as one.
			is_discount_line = not cint(d.is_return) and flt(it.amount) < 0
			if is_discount_line:
				item_discount += abs(flt(it.amount))
			else:
				item_discount += line_discount
			lines.append({
				"item": it.item_code,
				"name": it.item_name or it.item_code,
				"qty": flt(it.qty),
				"uom": it.uom,
				"list_rate": _r(list_rate) if recorded else _r(rate),
				"discount_percent": _r(it.discount_percentage) if recorded else 0.0,
				"discount": _r(line_discount),
				"rate": _r(rate),
				"amount": _r(it.amount),
				"is_discount": is_discount_line,
			})

		# Installments: what was due when, and — paying the earliest first —
		# how much of each is settled. The invoice's own outstanding amount is
		# the ground truth; the installments only divide it over time.
		settled = max(receivable - flt(d.outstanding_amount), 0.0) if not cint(d.is_return) else 0.0
		plan = schedule.get(name) or [frappe._dict(due_date=d.due_date or d.posting_date, payment_amount=receivable, description=None)]
		installments = []
		left = settled
		for i, s in enumerate(plan):
			amount = flt(s.payment_amount)
			paid = min(amount, max(left, 0.0))
			left -= paid
			outstanding = amount - paid
			due = getdate(s.due_date) if s.due_date else None
			status = (
				"paid" if outstanding <= 0.005
				else ("overdue" if due and due < now else ("partly" if paid > 0.005 else "due"))
			)
			installments.append({
				"number": i + 1,
				"of": len(plan),
				"due_date": str(s.due_date or ""),
				"amount": _r(amount),
				"paid": _r(paid),
				"outstanding": _r(outstanding),
				"status": status,
				"description": s.description,
			})

		out[name] = {
			"name": name,
			"date": str(d.posting_date or ""),
			"due_date": str(d.due_date or ""),
			"is_return": bool(cint(d.is_return)),
			"return_against": d.return_against,
			"is_opening": d.is_opening == "Yes",
			"is_pos": bool(cint(d.is_pos)),
			"student": d.student,
			"year": d.ms_academic_year,
			"term": d.ms_academic_term,
			"program": d.ms_program,
			"items": lines,
			"gross": _r(sum(l["list_rate"] * l["qty"] for l in lines if not l["is_discount"])),
			"item_discount": _r(item_discount),
			"invoice_discount": _r(d.discount_amount),
			"invoice_discount_percent": _r(d.additional_discount_percentage),
			"net_total": _r(d.net_total),
			"taxes": [
				{"description": t.description, "amount": _r(t.tax_amount_after_discount_amount or t.tax_amount)}
				for t in taxes.get(name, [])
			],
			"rounding": _r(d.rounding_adjustment) if not cint(d.disable_rounded_total) else 0.0,
			"total": _r(receivable),
			"outstanding": _r(d.outstanding_amount),
			"paid_at_sale": _r(d.paid_amount) if cint(d.is_pos) else 0.0,
			"status": d.status,
			"installments": installments if not cint(d.is_return) else [],
			"remarks": (d.remarks or "").strip() if d.remarks and d.remarks != "No Remarks" else "",
		}
	# A return has no term of its own on most sites; it belongs where the
	# invoice it reverses belongs.
	for inv in out.values():
		if inv["is_return"] and not inv["term"] and inv["return_against"] in out:
			origin = out[inv["return_against"]]
			inv["year"], inv["term"] = origin["year"], origin["term"]
	return out


def _origins(names: list[str], known: dict) -> dict:
	"""Year and term of invoices a return points at but which are not loaded."""
	missing = [n for n in names if n and n not in known]
	if not missing:
		return {}
	return {
		r.name: r
		for r in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", missing]},
			fields=["name", "ms_academic_year", "ms_academic_term"],
		)
	}


def _payments(names: list[str]) -> dict[str, dict]:
	if not names:
		return {}
	docs = {
		d.name: d
		for d in frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", names]},
			fields=[
				"name", "posting_date", "payment_type", "mode_of_payment", "reference_no",
				"reference_date", "paid_amount", "received_amount", "unallocated_amount",
				"remarks", "paid_from", "paid_to",
			],
			limit_page_length=0,
		)
	}
	refs: dict[str, list] = {}
	for r in frappe.get_all(
		"Payment Entry Reference",
		filters={"parent": ["in", names]},
		fields=["parent", "reference_doctype", "reference_name", "allocated_amount", "payment_term"],
		order_by="parent, idx",
		limit_page_length=0,
	):
		refs.setdefault(r.parent, []).append(r)
	out = {}
	for name, d in docs.items():
		out[name] = {
			"name": name,
			"date": str(d.posting_date or ""),
			"type": d.payment_type,
			"mode": d.mode_of_payment,
			"reference_no": d.reference_no,
			"reference_date": str(d.reference_date or ""),
			"amount": _r(d.paid_amount if d.payment_type == "Receive" else d.received_amount or d.paid_amount),
			"unallocated": _r(d.unallocated_amount),
			"allocations": [
				{
					"doctype": r.reference_doctype,
					"invoice": r.reference_name,
					"amount": _r(r.allocated_amount),
					"payment_term": r.payment_term,
				}
				for r in refs.get(name, [])
			],
		}
	return out


def _journals(names: list[str], customer: str) -> dict[str, dict]:
	if not names:
		return {}
	heads = {
		d.name: d
		for d in frappe.get_all(
			"Journal Entry",
			filters={"name": ["in", names]},
			fields=["name", "title", "voucher_type", "user_remark", "cheque_no"],
			limit_page_length=0,
		)
	}
	lines: dict[str, list] = {}
	for a in frappe.get_all(
		"Journal Entry Account",
		filters={"parent": ["in", names], "party_type": "Customer", "party": customer},
		fields=["parent", "reference_type", "reference_name", "debit", "credit"],
		limit_page_length=0,
	):
		lines.setdefault(a.parent, []).append(a)
	return {
		n: {
			"name": n,
			"title": h.title,
			"type": h.voucher_type,
			"remark": (h.user_remark or "").strip(),
			"cheque_no": h.cheque_no,
			"references": [
				{"doctype": a.reference_type, "invoice": a.reference_name, "amount": _r(flt(a.credit) - flt(a.debit))}
				for a in lines.get(n, [])
				if a.reference_type == "Sales Invoice" and a.reference_name
			],
		}
		for n, h in heads.items()
	}


def _period_key(year, term) -> str:
	if not year and not term:
		return OTHER
	return f"{year or ''}|{term or ''}"


def _period_label(year, term) -> str:
	if not year and not term:
		return OTHER_LABEL
	if term and year and year in term:
		return term
	return " — ".join(x for x in (term, year) if x)


MODE_AR = {"Cash": "نقداً", "Cheque": "شيك", "Bank Draft": "حوالة بنكية", "Wire Transfer": "تحويل بنكي", "Credit Card": "بطاقة ائتمان"}


def account(student: str) -> dict:
	"""One student's full statement of account."""
	name, customer = frappe.db.get_value("Student", student, ["student_name", "customer"]) or (student, None)
	currency = frappe.db.get_default("currency")
	base = {
		"student": student,
		"student_name": name,
		"customer": customer,
		"currency": currency,
	}
	if not customer:
		return {**base, **_empty(), "notice": "لا يوجد حساب مالي مرتبط بهذا الطالب بعد."}

	vouchers = _vouchers(customer)
	inv = _invoices([v.voucher_no for v in vouchers if v.voucher_type == "Sales Invoice"])
	pay = _payments([v.voucher_no for v in vouchers if v.voucher_type == "Payment Entry"])
	jes = _journals([v.voucher_no for v in vouchers if v.voucher_type == "Journal Entry"], customer)

	# Returns against invoices outside this account (rare) still get a term.
	extra = _origins([i["return_against"] for i in inv.values() if i["is_return"] and not i["term"]], inv)
	for i in inv.values():
		o = extra.get(i["return_against"])
		if o and not i["term"]:
			i["year"], i["term"] = o.ms_academic_year, o.ms_academic_term

	def inv_period(invoice_name):
		i = inv.get(invoice_name)
		return _period_key(i["year"], i["term"]) if i else OTHER

	ledger: list[dict] = []
	balance = 0.0

	def post(row):
		nonlocal balance
		balance = _r(balance + row["debit"] - row["credit"])
		row["balance"] = balance
		ledger.append(row)

	summary = {
		"billed": 0.0, "discounts": 0.0, "returns": 0.0, "paid": 0.0,
		"refunds": 0.0, "journal": 0.0, "opening": 0.0, "advances": 0.0,
	}

	for v in vouchers:
		debit, credit = _r(v.debit), _r(v.credit)
		date = str(v.posting_date or "")
		if v.voucher_type == "Sales Invoice":
			i = inv.get(v.voucher_no) or {}
			period = _period_key(i.get("year"), i.get("term"))
			label = _period_label(i.get("year"), i.get("term"))
			names = [l["name"] for l in i.get("items", []) if not l["is_discount"]]
			if i.get("is_return"):
				# A credit note: what it takes back is the credit; a refund paid
				# out at the counter for it is the debit.
				post({
					"date": date, "kind": "return", "reference": v.voucher_no,
					# The reversed invoice is shown in the detail, not in the title:
					# a document number inside Arabic text breaks across lines.
					"description": "مرتجع — " + ("، ".join(l["name"] for l in i.get("items", [])[:2]) or "إشعار دائن"),
					"period": label, "period_key": period, "due_date": "",
					"debit": 0.0, "credit": credit, "invoice": i,
				})
				summary["returns"] += credit
				if debit:
					post({
						"date": date, "kind": "refund", "reference": v.voucher_no,
						"description": "مبلغ مُعاد نقداً عن المرتجع", "period": label,
						"period_key": period, "due_date": "", "debit": debit, "credit": 0.0,
					})
					summary["refunds"] += debit
			else:
				kind = "opening" if i.get("is_opening") else "invoice"
				post({
					"date": date, "kind": kind, "reference": v.voucher_no,
					"description": "، ".join(names[:3]) + ("…" if len(names) > 3 else "") if names else ("رصيد افتتاحي" if kind == "opening" else "فاتورة"),
					"period": label, "period_key": period, "due_date": i.get("due_date", ""),
					"debit": debit, "credit": 0.0, "invoice": i,
				})
				summary["opening" if kind == "opening" else "billed"] += debit
				summary["discounts"] += flt(i.get("item_discount")) + flt(i.get("invoice_discount"))
				if credit:
					post({
						"date": date, "kind": "payment", "reference": v.voucher_no,
						"description": "مدفوع عند الشراء", "period": label, "period_key": period,
						"due_date": "", "debit": 0.0, "credit": credit,
						"payment": {
							"name": v.voucher_no, "date": date, "type": "Receive", "mode": None,
							"reference_no": None, "reference_date": "", "amount": credit, "unallocated": 0.0,
							"allocations": [{"doctype": "Sales Invoice", "invoice": v.voucher_no, "amount": credit, "payment_term": None}],
						},
					})
					summary["paid"] += credit
		elif v.voucher_type == "Payment Entry":
			p = pay.get(v.voucher_no) or {}
			how = " — ".join(x for x in (MODE_AR.get(p.get("mode"), p.get("mode")), p.get("reference_no")) if x)
			for a in p.get("allocations", []):
				a["period"] = _period_label(*(inv[a["invoice"]]["year"], inv[a["invoice"]]["term"])) if a["invoice"] in inv else ""
			if credit:
				post({
					"date": date, "kind": "payment", "reference": v.voucher_no,
					"description": "دفعة" + (f" ({how})" if how else ""), "period": "", "period_key": "",
					"due_date": "", "debit": 0.0, "credit": credit, "payment": p,
				})
				summary["paid"] += credit
				summary["advances"] += flt(p.get("unallocated")) if p.get("type") == "Receive" else 0.0
			if debit:
				post({
					"date": date, "kind": "refund", "reference": v.voucher_no,
					"description": "مبلغ مُعاد إلى وليّ الأمر" + (f" ({how})" if how else ""), "period": "",
					"period_key": "", "due_date": "", "debit": debit, "credit": 0.0, "payment": p,
				})
				summary["refunds"] += debit
		elif v.voucher_type == "Journal Entry":
			j = jes.get(v.voucher_no) or {}
			post({
				"date": date, "kind": "journal", "reference": v.voucher_no,
				"description": j.get("remark") or j.get("title") or "قيد يومية",
				"period": "", "period_key": "", "due_date": "",
				"debit": debit, "credit": credit, "journal": j,
			})
			summary["journal"] += debit - credit
		else:
			post({
				"date": date, "kind": "other", "reference": v.voucher_no,
				"description": v.voucher_type, "period": "", "period_key": "", "due_date": "",
				"debit": debit, "credit": credit,
			})
			summary["journal"] += debit - credit

	# --- By academic year and term ---------------------------------------------
	periods: dict[str, dict] = {}

	def bucket(key, year=None, term=None):
		if key not in periods:
			periods[key] = {
				"key": key, "year": year, "term": term,
				"label": _period_label(year, term) if key != OTHER else OTHER_LABEL,
				"billed": 0.0, "discounts": 0.0, "returns": 0.0, "paid": 0.0,
				"adjustments": 0.0, "outstanding": 0.0, "overdue": 0.0,
				"invoices": [], "payments": [],
			}
		return periods[key]

	now = getdate(today())
	for i in inv.values():
		b = bucket(_period_key(i["year"], i["term"]), i["year"], i["term"])
		b["invoices"].append(i["name"])
		if i["is_return"]:
			b["returns"] += abs(i["total"])
			# A credit note not yet set against anything is money back on account.
			b["outstanding"] += i["outstanding"]
		else:
			b["billed"] += i["total"]
			b["discounts"] += i["item_discount"] + i["invoice_discount"]
			b["outstanding"] += i["outstanding"]
			b["overdue"] += sum(x["outstanding"] for x in i["installments"] if x["status"] == "overdue")
			if i["paid_at_sale"]:
				b["paid"] += i["paid_at_sale"]
				b["payments"].append({"payment": i["name"], "date": i["date"], "amount": i["paid_at_sale"], "at_sale": True})
	for p in pay.values():
		for a in p["allocations"]:
			if a["invoice"] in inv:
				b = bucket(inv_period(a["invoice"]))
				b["paid"] += a["amount"]
				b["payments"].append({"payment": p["name"], "date": p["date"], "amount": a["amount"], "invoice": a["invoice"]})
	for j in jes.values():
		for ref in j["references"]:
			if ref["invoice"] in inv:
				bucket(inv_period(ref["invoice"]))["adjustments"] += ref["amount"]

	period_list = sorted(
		periods.values(),
		key=lambda b: (b["key"] == OTHER, b["year"] or "", b["term"] or ""),
	)
	for b in period_list:
		for k in ("billed", "discounts", "returns", "paid", "adjustments", "outstanding", "overdue"):
			b[k] = _r(b[k])

	# --- Installments across the account ----------------------------------------
	installments = []
	for i in inv.values():
		if i["is_return"]:
			continue
		for x in i["installments"]:
			installments.append({
				**x,
				"invoice": i["name"],
				"period": _period_label(i["year"], i["term"]),
				"description": x.get("description") or "، ".join(
					l["name"] for l in i["items"][:2] if not l["is_discount"]
				),
			})
	installments.sort(key=lambda x: (x["due_date"] or "9999", x["invoice"]))

	payments = sorted(pay.values(), key=lambda p: p["date"], reverse=True)
	# Paid at the counter counts as a payment too.
	for i in inv.values():
		if i["paid_at_sale"]:
			payments.append({
				"name": i["name"], "date": i["date"], "type": "Receive", "mode": None,
				"reference_no": None, "reference_date": "", "amount": i["paid_at_sale"],
				"unallocated": 0.0, "at_sale": True,
				"allocations": [{"doctype": "Sales Invoice", "invoice": i["name"], "amount": i["paid_at_sale"], "payment_term": None, "period": _period_label(i["year"], i["term"])}],
			})
	payments.sort(key=lambda p: p["date"], reverse=True)

	overdue_items = sum(x["outstanding"] for x in installments if x["status"] == "overdue")
	summary = {k: _r(v) for k, v in summary.items()}
	summary.update({
		"balance": _r(balance),
		# What is overdue can never exceed what is owed: credit sitting on the
		# account (an advance, an unused credit note) is set against it.
		"overdue": _r(min(overdue_items, max(balance, 0.0))),
		"invoices": sum(1 for i in inv.values() if not i["is_return"]),
		"payments_count": len(payments),
		"last_payment": max((p["date"] for p in payments), default=None),
		"credit_notes_open": _r(-sum(i["outstanding"] for i in inv.values() if i["is_return"] and i["outstanding"] < 0)),
	})

	# Invoices raised for this student on someone else's account: shown so the
	# family is not surprised, and kept out of this balance, which is the
	# account's own.
	elsewhere = [
		{
			"invoice": r.name, "customer": r.customer, "date": str(r.posting_date),
			"total": _r(r.grand_total), "outstanding": _r(r.outstanding_amount),
		}
		for r in frappe.get_all(
			"Sales Invoice",
			filters={"student": student, "docstatus": 1, "customer": ["!=", customer]},
			fields=["name", "customer", "posting_date", "grand_total", "outstanding_amount"],
			limit_page_length=0,
		)
	]

	return {
		**base,
		"summary": summary,
		"ledger": ledger,
		"periods": period_list,
		"installments": installments,
		"payments": payments,
		"elsewhere": elsewhere,
		"notice": None,
	}


def _empty() -> dict:
	return {
		"summary": {
			"billed": 0.0, "discounts": 0.0, "returns": 0.0, "paid": 0.0, "refunds": 0.0,
			"journal": 0.0, "opening": 0.0, "advances": 0.0, "balance": 0.0, "overdue": 0.0,
			"invoices": 0, "payments_count": 0, "last_payment": None, "credit_notes_open": 0.0,
		},
		"ledger": [], "periods": [], "installments": [], "payments": [], "elsewhere": [],
	}


def child_summary(student: str) -> dict:
	"""A child's position for the family report — the same figures, less detail."""
	a = account(student)
	s = a["summary"]
	return {
		"student": student,
		"student_name": a["student_name"],
		**s,
		# The names the first version of the app reads.
		"credited": s["returns"],
		"outstanding": s["balance"],
	}


def family(students: list[str], selected: str | None) -> dict:
	children = [child_summary(s) for s in students]
	keys = ("billed", "discounts", "returns", "paid", "refunds", "journal", "opening", "advances", "balance", "overdue", "credit_notes_open")
	overall = {k: _r(sum(c[k] for c in children)) for k in keys}
	overall.update({
		"student": "", "student_name": "",
		"invoices": sum(c["invoices"] for c in children),
		"payments_count": sum(c["payments_count"] for c in children),
		"last_payment": max((c["last_payment"] for c in children if c["last_payment"]), default=None),
		"credited": overall["returns"], "outstanding": overall["balance"],
	})
	out = {
		"currency": frappe.db.get_default("currency"),
		"children": children,
		"overall": overall,
		"student": None, "student_name": None,
		"ledger": [], "closing_balance": 0.0,
	}
	if selected:
		detail = account(selected)
		out.update(detail)
		out["closing_balance"] = detail["summary"]["balance"]
	return out


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_STUDENT, ROLE_PARENT)
def student_statement(student: str = None, persona: str = None):
	"""كشف الحساب: موقف كل الأبناء، وتفصيل الابن المختار كاملاً."""
	scope = resolve_scope(persona)
	students = students_for(persona, scope, student)
	if not students:
		return family([], None)
	selected = student if student in students else (students[0] if len(students) == 1 else None)
	if persona in (ROLE_ADMIN, ROLE_SECRETARY):
		selected = students[0]
	return family(students, selected)
