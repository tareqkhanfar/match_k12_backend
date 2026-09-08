
# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Rules that keep a school Sales Invoice sound, wherever it is created.

These run as document hooks rather than inside an API endpoint, so they apply
to an invoice typed into the ERPNext desk, imported, created by a script or
raised by this app's own screens. Accounting rules that only hold on one path
are not rules.

What is enforced:

  * an invoice naming a student must name the enrolment it belongs to
  * that enrolment must belong to that same student
  * the enrolment must not be cancelled
  * the customer must be the one linked to the student

Ordinary sales invoices — no student — are left completely alone.
"""

import frappe
from frappe import _
from frappe.utils import flt


def validate_student_invoice(doc, method=None):
	"""Hooked on Sales Invoice validate."""
	if not doc.get("student"):
		# Not a school invoice. A stray enrolment link would be meaningless,
		# so clear it rather than leave it dangling.
		if doc.get("ms_program_enrollment"):
			doc.ms_program_enrollment = None
		return

	if not frappe.db.exists("Student", doc.student):
		frappe.throw(_("Student {0} does not exist").format(doc.student))

	_require_enrollment(doc)
	_validate_enrollment_belongs_to_student(doc)
	_validate_customer_matches_student(doc)
	_copy_enrollment_context(doc)


def _require_enrollment(doc):
	if doc.get("ms_program_enrollment"):
		return

	# Fill it in when there is exactly one candidate; a school with a single
	# enrolment per student should not have to state the obvious.
	candidates = frappe.get_all(
		"Program Enrollment",
		filters={"student": doc.student, "docstatus": 1},
		pluck="name",
		order_by="creation desc",
	)
	if len(candidates) == 1:
		doc.ms_program_enrollment = candidates[0]
		return

	if not candidates:
		frappe.throw(
			_(
				"Student {0} has no submitted Program Enrollment. "
				"Enrol the student before invoicing."
			).format(frappe.bold(doc.student))
		)

	frappe.throw(
		_("Select the Program Enrollment this invoice belongs to ({0} available).").format(
			len(candidates)
		)
	)


def _validate_enrollment_belongs_to_student(doc):
	row = frappe.db.get_value(
		"Program Enrollment",
		doc.ms_program_enrollment,
		["student", "docstatus"],
		as_dict=True,
	)
	if not row:
		frappe.throw(
			_("Program Enrollment {0} does not exist").format(doc.ms_program_enrollment)
		)

	if row.student != doc.student:
		# The check the old Fees doctype made, and the reason it existed: an
		# invoice billed against another child's enrolment is silently wrong.
		frappe.throw(
			_("Program Enrollment {0} belongs to {1}, not to {2}").format(
				frappe.bold(doc.ms_program_enrollment),
				frappe.bold(row.student),
				frappe.bold(doc.student),
			)
		)

	if row.docstatus == 2:
		frappe.throw(
			_("Program Enrollment {0} is cancelled").format(
				frappe.bold(doc.ms_program_enrollment)
			)
		)


def _validate_customer_matches_student(doc):
	"""The receivable must sit on the student's own customer account."""
	customer = frappe.db.get_value("Student", doc.student, "customer")
	if not customer:
		student = frappe.get_doc("Student", doc.student)
		student.set_missing_customer_details()
		customer = frappe.db.get_value("Student", doc.student, "customer")

	if not customer:
		frappe.throw(
			_("Student {0} has no linked Customer, so no receivable can be booked").format(
				frappe.bold(doc.student)
			)
		)

	if not doc.customer:
		doc.customer = customer
	elif doc.customer != customer:
		frappe.throw(
			_(
				"Customer {0} is not the customer of student {1} ({2}). "
				"The receivable would be booked against the wrong account."
			).format(
				frappe.bold(doc.customer), frappe.bold(doc.student), frappe.bold(customer)
			)
		)


def _copy_enrollment_context(doc):
	"""Denormalise programme/year/term so reports can group without a join."""
	row = frappe.db.get_value(
		"Program Enrollment",
		doc.ms_program_enrollment,
		["program", "academic_year", "academic_term"],
		as_dict=True,
	)
	if not row:
		return
	doc.ms_program = row.program
	doc.ms_academic_year = row.academic_year
	doc.ms_academic_term = row.academic_term


# ---------------------------------------------------------------------------
# ما تحتاجه شاشة الفاتورة لتملأ نفسها
#
# القواعد أعلاه تحرس الفاتورة عند الحفظ، وهذه تُعينها قبله. الفارق مقصود:
# الحارس يرفض الخطأ، وهذه تجعل ارتكابه غير وارد أصلاً — فمن يفتح فاتورة من
# ملف طالب يجد العميل وتسجيله وبنوده جاهزة بدل أن يبحث عنها ثم يُخطئ فيها.
# ---------------------------------------------------------------------------


@frappe.whitelist()
def student_context(student: str) -> dict:
	"""عميل الطالب والسياق الدراسي الحالي، لتعبئة رأس الفاتورة.

	العميل يُنشأ عند الحاجة: طالبٌ سُجّل ولم يُفتح له حساب عميل بعد يجب أن
	تُفتح له فاتورة لا أن تُرفض — وهذا ما تفعله `set_missing_customer_details`
	في Education نفسها.
	"""
	if not student or not frappe.db.exists("Student", student):
		return {}

	customer = frappe.db.get_value("Student", student, "customer")
	if not customer:
		doc = frappe.get_doc("Student", student)
		doc.set_missing_customer_details()
		customer = frappe.db.get_value("Student", student, "customer")

	return {
		"customer": customer,
		"student_name": frappe.db.get_value("Student", student, "student_name"),
		"academic_year": frappe.db.get_single_value(
			"Education Settings", "current_academic_year"
		),
		"academic_term": frappe.db.get_single_value(
			"Education Settings", "current_academic_term"
		),
	}


def _fee_structure_for(enrollment: dict) -> str | None:
	"""خطة الرسوم التي تنطبق على تسجيل بعينه.

	تُجرَّب من الأخصّ إلى الأعمّ: خطة تطابق الفئة والفصل معاً هي الأدق، ثم
	ما يهمل الفئة، ثم ما يهمل الفصل. بلا هذا التدرّج تعود مدرسةٌ لا تستعمل
	فئات الطلاب بلا خطة أصلاً، ويُفتح لها إيصال فارغ.
	"""
	base = {"program": enrollment.get("program"), "docstatus": ["<", 2]}

	attempts = [
		{
			**base,
			"academic_year": enrollment.get("academic_year"),
			"academic_term": enrollment.get("academic_term"),
			"student_category": enrollment.get("student_category"),
		},
		{
			**base,
			"academic_year": enrollment.get("academic_year"),
			"academic_term": enrollment.get("academic_term"),
		},
		{**base, "academic_year": enrollment.get("academic_year")},
	]

	for filters in attempts:
		clean = {k: v for k, v in filters.items() if v}
		if "program" not in clean:
			continue
		found = frappe.get_all(
			"Fee Structure", filters=clean, pluck="name", order_by="modified desc", limit=1
		)
		if found:
			return found[0]
	return None


@frappe.whitelist()
def enrollment_items(program_enrollment: str) -> dict:
	"""بنود الفاتورة المستمدّة من خطة رسوم هذا التسجيل.

	كل مكوّن رسوم يحمل صنفاً ونسبة خصم، فيتحوّل صفّاً في الفاتورة كما هو —
	لا إعادة إدخال يدوي ولا فرصة لخطأ في مبلغ.
	"""
	if not program_enrollment:
		return {"items": []}

	enrollment = frappe.db.get_value(
		"Program Enrollment",
		program_enrollment,
		["student", "program", "academic_year", "academic_term", "student_category"],
		as_dict=True,
	)
	if not enrollment:
		return {"items": []}

	structure = _fee_structure_for(enrollment)
	if not structure:
		return {
			"items": [],
			"message": _("No fee structure found for programme {0}").format(
				enrollment.get("program")
			),
		}

	items = []
	for row in frappe.get_all(
		"Fee Component",
		filters={"parent": structure, "parenttype": "Fee Structure"},
		fields=["fees_category", "description", "amount", "item", "discount"],
		order_by="idx asc",
	):
		# مكوّن بلا صنف لا يصلح بنداً في فاتورة، فنتخطّاه ونسمّيه في التحذير
		# بدل أن نضع صفّاً لا يُحفظ.
		if not row.item:
			continue
		gross = flt(row.amount)
		discount = flt(row.discount)
		# الصافي يُحسب هنا لا يُترك لـERPNext: هي لا تعيد حساب `rate` متى
		# كان مضبوطاً — والسعر يجب أن يُضبط وإلا استُبدل بسعر قائمة الأسعار.
		# فنرسل الثلاثة متّسقة: الإجمالي، والنسبة، والصافي بينهما.
		net = flt(gross * (1 - discount / 100.0), 2) if discount else gross
		items.append(
			{
				"item_code": row.item,
				"description": row.description or row.fees_category,
				"qty": 1,
				"price_list_rate": gross,
				"discount_percentage": discount,
				"rate": net,
			}
		)

	skipped = [
		r.fees_category
		for r in frappe.get_all(
			"Fee Component",
			filters={"parent": structure, "parenttype": "Fee Structure", "item": ["is", "not set"]},
			fields=["fees_category"],
		)
	]

	return {
		"items": items,
		"fee_structure": structure,
		"program": enrollment.get("program"),
		"academic_year": enrollment.get("academic_year"),
		"academic_term": enrollment.get("academic_term"),
		"skipped": skipped,
	}


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def enrollment_query(doctype, txt, searchfield, start, page_len, filters):
	"""قائمة التسجيلات المعروضة في حقل الفاتورة.

	الطالب شرطٌ لا يسقط: اختيار تسجيل طالب آخر يرفضه الخادم بعد أن يكون
	المستخدم ملأ الفاتورة كلها. أما السنة والفصل فيضيّقان القائمة ولا
	يفرغانها — فإن لم يطابقهما شيء عُرضت تسجيلات الطالب كلها.

	السبب من الواقع لا من الاحتياط: مدرسةٌ فتحت سنة جديدة وطلابها ما زالوا
	مسجَّلين في شُعب السنة الماضية، فالتصفية بالفصل الحالي وحدها تُخلي
	القائمة ولا يفهم المستخدم لماذا لا يجد تسجيل طالبٍ يراه أمامه.
	"""
	filters = filters or {}
	student = filters.get("student")
	if not student:
		return []

	base = {"student": student, "docstatus": 1}
	if txt:
		base["name"] = ["like", f"%{txt}%"]

	narrow = dict(base)
	for key in ("academic_year", "academic_term", "program"):
		if filters.get(key):
			narrow[key] = filters[key]

	def fetch(where):
		return frappe.get_all(
			"Program Enrollment",
			filters=where,
			fields=["name", "program", "academic_year", "academic_term"],
			order_by="academic_year desc, modified desc",
			start=start,
			page_length=page_len,
		)

	rows = fetch(narrow) if narrow != base else []
	if not rows:
		rows = fetch(base)

	return [
		[r.name, r.program or "", r.academic_term or r.academic_year or ""] for r in rows
	]
