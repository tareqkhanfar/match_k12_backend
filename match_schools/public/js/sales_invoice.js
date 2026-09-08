// فاتورة مبيعات لطالب: تملأ نفسها بدل أن تُملأ يدوياً.
//
// القواعد الخادمية في `ms_billing.py` ترفض الفاتورة الخاطئة عند الحفظ. هذا
// الملف يجعل ارتكاب الخطأ غير وارد أصلاً: العميل يأتي مع الطالب، وقائمة
// التسجيلات لا تعرض إلا ما يخصّه، وبنود الرسوم تُجلب من خطتها بمبالغها
// وخصومها. الفارق بين الاثنين مقصود — الحارس يبقى حتى لو أُنشئت الفاتورة من
// استيراد أو سكربت لا يمرّ بهذه الشاشة.

frappe.ui.form.on("Sales Invoice", {
	setup(frm) {
		// التسجيلات المعروضة تخصّ هذا الطالب وهذه الفترة وحدها. بلا التصفية
		// تُعرض ٣٣٦ تسجيلاً لكل الطلاب، واختيار تسجيل طالب آخر يرفضه الخادم
		// بعد أن يكون المستخدم ملأ الفاتورة كلها.
		frm.set_query("ms_program_enrollment", function (doc) {
			return {
				query: "match_schools.ms_billing.enrollment_query",
				filters: {
					student: doc.student,
					academic_year: doc.ms_academic_year,
					academic_term: doc.ms_academic_term,
					program: doc.ms_program,
				},
			};
		});
	},

	onload(frm) {
		// الفاتورة المفتوحة من روابط الطالب تصل والطالب مضبوط سلفاً، فلا
		// يعمل مشغّل الحقل. بلا هذا يجد المستخدم اسم الطالب وخانة العميل
		// فارغة — وهو أول ما تشكو منه الشاشة.
		if (frm.is_new() && frm.doc.student && !frm.doc.customer) {
			fill_student_context(frm);
		}
	},

	student(frm) {
		if (!frm.doc.student) return;

		// تغيير الطالب يُبطل تسجيله السابق: تركه يعني فاتورةً باسم طالب
		// وتسجيلٍ لآخر، وهو ما يرفضه الخادم عند الحفظ.
		frm.set_value("ms_program_enrollment", null);
		fill_student_context(frm);
	},

	ms_program_enrollment(frm) {
		if (!frm.doc.ms_program_enrollment) return;
		fetch_fee_items(frm);
	},
});

/** يملأ العميل والسياق الدراسي من الطالب. */
function fill_student_context(frm) {
	frappe.call({
		method: "match_schools.ms_billing.student_context",
		args: { student: frm.doc.student },
		callback(r) {
			const d = r.message || {};
			if (d.customer) frm.set_value("customer", d.customer);
			// السنة والفصل يُملآن ليصفّيا قائمة التسجيلات، ولا يُكتب فوق
			// اختيار صريح سبقهما.
			if (d.academic_year && !frm.doc.ms_academic_year) {
				frm.set_value("ms_academic_year", d.academic_year);
			}
			if (d.academic_term && !frm.doc.ms_academic_term) {
				frm.set_value("ms_academic_term", d.academic_term);
			}
		},
	});
}

/** يجلب بنود خطة الرسوم ويضعها في الجدول. */
function fetch_fee_items(frm) {
	frappe.call({
		method: "match_schools.ms_billing.enrollment_items",
		args: { program_enrollment: frm.doc.ms_program_enrollment },
		freeze: true,
		freeze_message: __("جارٍ جلب بنود الرسوم…"),
		callback(r) {
			const d = r.message || {};

			if (!d.items || !d.items.length) {
				frappe.show_alert({
					message: d.message || __("لا توجد خطة رسوم لهذا التسجيل"),
					indicator: "orange",
				});
				return;
			}

			const filled = (frm.doc.items || []).filter((row) => row.item_code);
			if (!filled.length) {
				apply_items(frm, d);
				return;
			}

			// بنودٌ مكتوبة سلفاً لا تُمحى بلا إذن: قد تكون رسماً يدوياً
			// أضافه المحاسب، وفقدانه صامتاً أسوأ من عدم الجلب.
			frappe.confirm(
				__("الفاتورة تحتوي {0} بنداً. استبدالها ببنود خطة الرسوم؟", [filled.length]),
				() => apply_items(frm, d),
			);
		},
	});
}

function apply_items(frm, d) {
	frm.clear_table("items");
	(d.items || []).forEach((row) => {
		const child = frm.add_child("items");
		child.item_code = row.item_code;
		child.description = row.description;
		// إسناد مباشر لا عبر `set_value`: الأخير يُشغّل جلب بيانات الصنف
		// فيستبدل السعر بسعر قائمة الأسعار ويضيع مبلغ خطة الرسوم.
		child.qty = row.qty;
		child.price_list_rate = row.price_list_rate;
		child.discount_percentage = row.discount_percentage;
		child.rate = row.rate;
	});
	frm.refresh_field("items");

	if (d.skipped && d.skipped.length) {
		frappe.msgprint({
			title: __("مكوّنات لم تُضَف"),
			indicator: "orange",
			message: __("هذه المكوّنات بلا صنف مرتبط، فلم تُضَف: {0}", [
				d.skipped.join("، "),
			]),
		});
	}

	frappe.show_alert({
		message: __("أُضيف {0} بنداً من {1}", [d.items.length, d.fee_structure]),
		indicator: "green",
	});
}
