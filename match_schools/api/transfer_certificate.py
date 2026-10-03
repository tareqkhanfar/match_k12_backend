"""The ministry's student-transfer certificate, as a form design.

Kept here rather than only in a school's own form list, so a transfer can be
previewed and printed on every site without anyone building the form first.
If a school has its own «شهادة انتقال الطالب» form (category school), that one
is used instead - same field names - so its edits to the layout apply. Either
can be replaced from Settings › تصاميم الطباعة (see print_designs).

The certificate's top is the school's letterhead image (`banner()`), the same
strip its letters carry, rather than the ministry lines typed out as text.
"""

import re

TITLE = "شهادة انتقال الطالب"
CSS = '\n.tc-head { display: grid; grid-template-columns: 1fr auto 1fr; align-items: start; gap: 6mm; font-size: 12.5px; line-height: 1.45; font-weight: 700; }\n.tc-head .en { direction: ltr; text-align: left; }\n.tc-head .mid { text-align: center; }\n.tc-head .mid .no { display: inline-block; margin-top: 2mm; padding: 1mm 5mm; background: #fff3a8; border: 1px solid #c9b64a; }\n.tc-meta { width: 60mm; margin-top: 1mm; font-size: 12.5px; line-height: 1.5; }\n.tc-meta .val, .tc-sign .val, .tc-cell .v { unicode-bidi: plaintext; }\n.tc-title { text-align: center; font-size: 22px; font-weight: 800; letter-spacing: 3px; margin: 1mm 0 2mm; }\n.tc-grid { display: grid; grid-template-columns: 1fr 1fr; column-gap: 8mm; font-size: 12.5px; line-height: 1.6; }\n.tc-cell { display: flex; align-items: baseline; gap: 2mm; padding: 0.9mm 0; }\n.tc-cell.full { grid-column: 1 / -1; }\n.tc-cell .n { background: #d8d8d8; min-width: 8mm; text-align: center; font-weight: 700; border-radius: 2px; }\n.tc-cell b { white-space: nowrap; font-weight: 700; }\n.tc-cell .v { flex: 1; border-bottom: 1.2px dashed #444; min-height: 1.3em; padding: 0 2mm; font-weight: 500; }\n.tc-cell small { color: #555; font-size: 10.5px; }\n.tc-sign { display: grid; grid-template-columns: 1fr 1fr; column-gap: 8mm; margin-top: 2mm; font-size: 12.5px; line-height: 1.6; }\n.tc-seal { box-sizing: border-box; width: 100%; border: 1.5px solid #333; height: 26mm; margin-top: 3mm; padding: 2mm 3mm; font-weight: 700; font-size: 13px; }\n.tc-notes { font-size: 10px; line-height: 1.5; margin-top: 2mm; }\n.tc-notes ol { margin: 0; padding-inline-start: 6mm; }\n.tc-notes ol ol { list-style: arabic-indic; }\n.tc-foot { display: flex; justify-content: space-between; border-top: 1.5px solid #333; margin-top: 3mm; padding-top: 1.5mm; font-size: 10.5px; font-weight: 700; }\n.tc-foot .en { direction: ltr; }\n'
DESIGN = '{% macro row(n, label, value, full=false, note="") -%}\n<div class="tc-cell{{ \' full\' if full }}"><span class="n">{{ n }}</span><b>{{ label }}:</b><span class="v">{{ value or \'\' }}</span>{% if note %}<small>{{ note }}</small>{% endif %}</div>\n{%- endmacro %}\n{{ banner() }}\n<div class="tc-meta">\n  {{ info("الرقم", values.number) }}\n  {{ info("التاريخ", values.cert_date or filled_date) }}\n</div>\n<div class="tc-title">شهادة انتقال الطالب</div>\n<div class="tc-grid">\n  <div class="tc-cell"><b>من مدرسة:</b><span class="v">{{ school }}</span></div>\n  <div class="tc-cell"><b>إلى مدرسة / محافظة:</b><span class="v">{{ values.to_school }}</span></div>\n  {{ row("1", "اسم الطالب / الطالبة (رباعيًا)", values.full_name or student.name or student_name, true) }}\n  {{ row("2", "تاريخ ولادة الطالب/ة", values.dob or student.date_of_birth) }}\n  {{ row("3", "مكان الولادة", values.birthplace) }}\n  {{ row("4", "اسم ولي أمر الطالب/ة", values.guardian or student.guardian) }}\n  {{ row("5", "الديانة", values.religion) }}\n  {{ row("6", "عمل الوالد أو ولي الأمر", values.guardian_job) }}\n  {{ row("7", "عنوان الوالد أو ولي الأمر", values.guardian_address or student.city) }}\n  {{ row("8", "المدارس السابقة التي تعلّم/ت فيها", values.previous_schools, true) }}\n  {{ row("9", "تاريخ دخول الطالب/ة الصف الأول الأساسي", values.first_grade_date, true) }}\n  {{ row("10", "تاريخ دخوله/ها المدرسة الحالية", values.joined or student.joining_date) }}\n  {{ row("11", "الصف الذي دخله (دخلته)", values.joined_grade) }}\n  {{ row("12", "الصفوف التي أعادها (أعادتها) الطالب/ة", values.repeated, true) }}\n  {{ row("13", "الصف الحالي", values.current_grade or student.program) }}\n  {{ row("14", "العام الدراسي", values.year or student.academic_year) }}\n  {{ row("15", "السلوك", values.conduct) }}\n  {{ row("16", "عدد أيام غياب الطالب/ة في الصف الحالي", values.absence) }}\n  {{ row("17", "عدد أيام الغياب المشروعة", values.absence_excused, false, "لطلبة الصف الثاني الثانوي فقط") }}\n  <div class="tc-cell"><b>غير المشروعة:</b><span class="v">{{ values.absence_unexcused }}</span></div>\n  {{ row("18", "المرحلة / الفرع", values.stream, true) }}\n  {{ row("19", "ملحوظات", values.notes, true) }}\n</div>\n<div class="tc-sign">\n  {{ info("التاريخ", values.cert_date or filled_date) }}\n  {{ info("اسم مدير/ة المدرسة", values.principal or \'\') }}\n  {{ info("توقيع مدير/ة المدرسة", "") }}\n</div>\n<div class="tc-seal">للتصديق</div>\n<div class="tc-notes"><ol>\n  <li>لا يجوز إعطاء شهادة الانتقال دون ذكر اسم المدرسة التي يريد الطالب الانتقال إليها داخل المحافظة، أما إذا كان النقل إلى خارج المحافظة فيذكر اسم تلك المحافظة المنقول إليها، وإذا كان النقل إلى خارج محافظات الوطن فيكتب «إلى مدارس البلد المنقول إليه الطالب».</li>\n  <li>يذكر في البند (19) وضع الطالب الأكاديمي من خلال ذكر علامات الطالب كما يلي:\n    <ol>\n      <li>إذا كان النقل خلال الفصل الأول تكتب علامات منتصف الفصل إن وجدت وملاحظات عن وضع الطالب الأكاديمي.</li>\n      <li>إذا كان النقل بعد الفصل الأول تكتب ملاحظات عن وضع الطالب الأكاديمي ويرفق كشف بعلامات الفصل الأول مكربن من الخلف إذا كان بخط اليد، أو مطبوع على الحاسوب.</li>\n      <li>إذا كان النقل بعد منتصف الفصل الثاني تكتب علامات منتصف الفصل الثاني إن وجدت وملاحظات عن وضع الطالب الأكاديمي ويرفق كشف علامات الفصل الأول مكربن من الخلف.</li>\n      <li>إذا كان النقل بعد انتهاء الفصل الدراسي الثاني تكتب ملاحظات عن وضع الطالب الأكاديمي وترفق شهادة بعلامات الطالب في السنة الدراسية كاملة.</li>\n    </ol></li>\n  <li>تكتب التواريخ بالتقويم الغربي (الميلادي).</li>\n  <li>يجب كتابة شهادة الانتقال على كربون من الخلف أو مطبوعة على الحاسوب.</li>\n</ol></div>\n'
# (fieldname, label, fieldtype, options, default, reqd, width, description)
FIELDS = [('number', 'رقم الشهادة', 'Data', '', '', 0, 'half', 'مثال: 3/12'),
 ('cert_date', 'تاريخ الشهادة', 'Date', '', '', 0, 'half', 'فارغ = تاريخ التعبئة'),
 ('to_school',
  'إلى مدرسة / محافظة',
  'Data',
  '',
  '',
  1,
  'full',
  'اسم المدرسة داخل المحافظة، أو اسم المحافظة إن كان النقل خارجها'),
 ('full_name', 'اسم الطالب رباعيًا', 'Data', '', '', 0, 'full', 'فارغ = الاسم في سجل الطالب'),
 ('dob', 'تاريخ الولادة', 'Data', '', '', 0, 'half', 'فارغ = من سجل الطالب'),
 ('birthplace', 'مكان الولادة', 'Data', '', '', 0, 'half', ''),
 ('guardian', 'اسم ولي الأمر', 'Data', '', '', 0, 'half', 'فارغ = من سجل الطالب'),
 ('religion', 'الديانة', 'Data', '', 'الإسلام', 0, 'half', ''),
 ('guardian_job', 'عمل الوالد أو ولي الأمر', 'Data', '', '', 0, 'half', ''),
 ('guardian_address', 'عنوان الوالد أو ولي الأمر', 'Data', '', '', 0, 'half', ''),
 ('previous_schools', 'المدارس السابقة', 'Data', '', '', 0, 'full', ''),
 ('first_grade_date', 'تاريخ دخول الصف الأول الأساسي', 'Data', '', '', 0, 'half', ''),
 ('joined', 'تاريخ دخول المدرسة الحالية', 'Data', '', '', 0, 'half', 'فارغ = تاريخ الالتحاق في سجل الطالب'),
 ('joined_grade', 'الصف الذي دخله', 'Data', '', '', 0, 'half', ''),
 ('repeated', 'الصفوف التي أعادها', 'Data', '', '', 0, 'half', ''),
 ('current_grade', 'الصف الحالي', 'Data', '', '', 0, 'half', 'فارغ = من سجل الطالب'),
 ('year', 'العام الدراسي', 'Data', '', '', 0, 'half', 'مثال: 2025/2026'),
 ('conduct', 'السلوك', 'Select', 'ممتاز\nجيد جدًا\nجيد\nمقبول', '', 0, 'half', ''),
 ('absence', 'أيام الغياب في الصف الحالي', 'Number', '', '', 0, 'half', ''),
 ('absence_excused', 'أيام الغياب المشروعة', 'Number', '', '', 0, 'half', 'لطلبة الصف الثاني الثانوي فقط'),
 ('absence_unexcused',
  'أيام الغياب غير المشروعة',
  'Number',
  '',
  '',
  0,
  'half',
  'لطلبة الصف الثاني الثانوي فقط'),
 ('stream', 'المرحلة / الفرع', 'Data', '', '', 0, 'half', ''),
 ('notes',
  'ملحوظات',
  'Long Text',
  '',
  '',
  0,
  'full',
  'وضع الطالب الأكاديمي — انظر التعليمات في أسفل الشهادة'),
 ('principal', 'اسم مدير المدرسة', 'Data', '', '', 0, 'half', '')]

# How the certificate prints until the school designs its own (theme, page).
PRINT_DEFAULT = {"design": DESIGN, "css": CSS, "theme": "classic", "orientation": "Portrait"}

# The ministry lines typed out as a three-column header, the way the
# certificate used to open — and the way a school's own copy of it still may.
_TEXT_HEAD = re.compile(r'<div class="tc-head">(?:\s*<div[^>]*>.*?</div>)*\s*</div>\n?', re.S)


def with_letterhead(design: str) -> str:
	"""A school's own certificate design, with the letterhead image as its top.

	The school asked for its letterhead on the certificate while keeping the
	rest exactly as it is; a copy of the form it made earlier still opens with
	the typed header, so only that block is swapped. A design that already
	draws something else at the top is left alone.
	"""
	if not design or "banner()" in design:
		return design
	return _TEXT_HEAD.sub("{{ banner() }}\n", design, count=1)
