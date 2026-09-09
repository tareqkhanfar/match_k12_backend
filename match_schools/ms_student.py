# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""تجاوز متحكّم الطالب: بريدٌ اختياري لا يمنع التسجيل.

Education تُلزم ببريد للطالب وتُنشئ منه حساب موقع عند كل حفظ. وهذا يفترض أن
لكل طالب بريداً — وهو غير صحيح في مدرسة أساسية: طفل الصف الأول لا بريد له،
وبريد وليّ أمره يخصّ وليّ الأمر لا الطالب، ووضعُه هنا يجعل حسابين يتنازعان
عنواناً واحداً.

رفعُ الإلزام وحده لا يكفي: `validate_user` تمضي إلى إنشاء المستخدم بعنوان
`None` فتنهار بـ AttributeError لا يدلّ على شيء. فيُتجاوز الصنف ليتخطّى
إنشاء الحساب حين لا بريد، ويبقى كما هو حين يوجد.
"""

from education.education.doctype.student.student import Student


class MSStudent(Student):
	def validate_user(self):
		"""لا حساب بلا بريد — وبقية السلوك كما هو."""
		if not (self.student_email_id or "").strip():
			# الحقل الفريد الفارغ يُخزَّن NULL لا "" ، فلا يتصادم طالبان بلا
			# بريد على فهرس التفرّد. تصفيره هنا يجعل ذلك صريحاً.
			self.student_email_id = None
			return

		super().validate_user()
