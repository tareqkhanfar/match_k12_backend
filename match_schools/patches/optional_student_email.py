"""اجعل بريد الطالب اختيارياً.

Education تُلزم به، وهو افتراضٌ لا يصحّ في مدرسة أساسية: طفل الصف الأول لا
بريد له، وبريد وليّ أمره يخصّ وليّ الأمر — ووضعُه على الطالب يجعل حسابين
يتنازعان عنواناً واحداً على حقلٍ فريد.

ولم يكن الإلزام هو العائق الوحيد: رفعُه وحده ينقل الخطأ إلى `validate_user`
التي تُنشئ حساباً بعنوان `None`. لذلك يرافق هذا الترقيعَ تجاوزُ الصنف في
`ms_student.py`.

الحقل يبقى **فريداً**: مدرسةٌ تسجّل بريداً يجب ألّا تسجّله لطالبين. وFrappe
يخزّن الحقل الفريد الفارغ NULL لا "" ، وفهرس التفرّد يقبل NULL متعدّدة —
فطلابٌ بلا بريد لا يتصادمون.
"""

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute():
	make_property_setter(
		"Student",
		"student_email_id",
		"reqd",
		0,
		"Check",
		validate_fields_for_doctype=False,
	)

	# سجلّات قديمة قد تحمل "" بدل NULL — لو مرّت من استيراد لا من الواجهة.
	# تركها يجعل الطالب الثاني بسلسلة فارغة يصطدم بفهرس التفرّد.
	frappe.db.sql(
		"""UPDATE `tabStudent` SET student_email_id = NULL
		   WHERE student_email_id = '' """
	)

	frappe.clear_cache(doctype="Student")
