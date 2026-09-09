"""أعطِ مساحة العمل `type` — وهو حقل إلزامي وصلت بدونه.

الملف الأصلي أُنشئ بلا `type`، فالسجلّ NULL. والأثر ليس تجميلياً: أي حفظ
للمساحة يفشل بخطأ حقل مطلوب — من الكود ومن الواجهة معاً. فمن أراد تقييد
ظهورها بدورٍ لم يستطع حفظ التقييد أصلاً.

ولا يكفي تصحيح الملف: `bench migrate` لا يعيد استيراد المساحة ما لم يتغيّر
ختم `modified` فيها، والمواقع القائمة تبقى على NULL. فيُضبط هنا مباشرةً.
"""

import frappe


def execute():
	for name in frappe.get_all(
		"Workspace", filters={"type": ["in", ["", None]]}, pluck="name"
	):
		frappe.db.set_value("Workspace", name, "type", "Workspace", update_modified=False)

	frappe.clear_cache(doctype="Workspace")
