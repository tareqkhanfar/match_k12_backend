"""The admission-approval letter («متسع») as a form design.

Built in, so a request can be previewed and printed on every site. A school
with its own «قبول طالب (متسع)» form (category school) gets that one instead —
same field names — so its edits to the letterhead and wording apply. Either
can be replaced from Settings › تصاميم الطباعة (see print_designs); the
letterhead `banner()` draws is the school's own image unless the design
there names another.
"""

TITLE = "قبول طالب (متسع)"
CSS = '\n.lt-top { margin-top: 5mm; font-weight: 700; }\n.lt-greet { margin-top: 7mm; }\n.lt-subject { text-align: center; font-weight: 800; font-size: 18px; text-decoration: underline; text-underline-offset: 6px; margin: 3mm 0 7mm; }\n.lt-body { text-align: justify; text-decoration: underline; text-underline-offset: 6px; text-decoration-thickness: 1px; margin: 0; }\n.lt-body b { font-weight: 800; }\n.lt-dec { margin-top: 8mm; text-decoration: underline; text-underline-offset: 6px; }\nol.lt-list { margin: 2mm 0 0; padding-inline-start: 7mm; }\nol.lt-list li { margin: 1.5mm 0; text-decoration: underline; text-underline-offset: 6px; text-decoration-thickness: 1px; }\n.lt-close { text-align: center; margin-top: 14mm; font-size: 16px; }\n.lt-seal { text-align: center; margin-top: 3mm; }\n.lt-sign { margin-top: 12mm; width: 62mm; margin-inline-start: auto; line-height: 2.1; font-size: 16px; }\n.lt-docs { margin-top: 10mm; }\n.lt-docs h4 { font-size: 17px; margin: 0 0 2mm; font-weight: 700; }\n.lt-docs ol { margin: 0; padding-inline-start: 7mm; }\n.lt-docs li { margin: 1.2mm 0; }\n.dots { min-width: 45mm; }\n.dt { unicode-bidi: isolate; direction: ltr; display: inline-block; }\n'
DESIGN = '{{ banner() }}\n<div class="lt-top">التاريخ: <span class="dt">{{ values.letter_date or filled_date or \'<span class="dots"></span>\' }}</span>م</div>\n<div class="lt-greet">تحية طيبة وبعد،،</div>\n\n{% set who = values.student_word or "الطالب" %}\n<div class="lt-subject">الموضوع: متسع</div>\n<p class="lt-body" style="text-decoration:none">لا مانع من قبول {{ who }}: <b>{{ field("اسم الطالب") }}</b> في الصف: <b>{{ field("الصف") }}</b> للعام الدراسي {{ field("العام الدراسي") }}م.</p>\n<div class="lt-close">مع الاحترام</div>\n<div class="lt-seal">الخاتم</div>\n<div class="lt-sign">مدير المدرسة<br>{{ values.principal or \'\' }}</div>\n{% set ALL_DOCS = [\'شهادة انتقال من المدرسة التي يدرس فيها الطالب\', \'صورة عن شهادة ميلاد الطالب\', \'6 صور شخصية\', \'صورة هوية الوالدين مفتوحة الملحق\', \'ملف الطالب التراكمي + شهادة نهاية العام الدراسي السابق بالعلامات\', \'تصديق شهادة الانتقال من مكتب التربية والتعليم الذي تتبع له المدرسة المنقولة منها الطالب\'] %}\n<div class="lt-docs"><h4>الوثائق المطلوبة:</h4>\n{% if blank %}{{ checks("الوثائق المطلوبة") }}{% else %}<ol>{% for d in (chosen("الوثائق المطلوبة") or ALL_DOCS) %}<li>{{ d }}</li>{% endfor %}</ol>{% endif %}\n</div>\n'
# How the letter prints until the school designs its own (theme, page).
PRINT_DEFAULT = {"design": DESIGN, "css": CSS, "theme": "letter", "orientation": "Portrait"}
# (fieldname, label, fieldtype, options, default, reqd, width, description)
FIELDS = [('letter_date', 'تاريخ الخطاب', 'Date', '', '', 0, 'half', 'يُترك فارغاً ليُطبع تاريخ التعبئة'),
 ('student_word', 'الطالب / الطالبة', 'Select', 'الطالب\nالطالبة', 'الطالب', 1, 'half', ''),
 ('applicant', 'اسم الطالب', 'Data', '', '', 1, 'full', 'الاسم كما سيُكتب في الخطاب'),
 ('grade', 'الصف', 'Data', '', '', 1, 'half', 'مثال: التاسع'),
 ('year', 'العام الدراسي', 'Data', '', '2026/2027', 1, 'half', ''),
 ('documents',
  'الوثائق المطلوبة',
  'Multi Select',
  'شهادة انتقال من المدرسة التي يدرس فيها الطالب\n'
  'صورة عن شهادة ميلاد الطالب\n'
  '6 صور شخصية\n'
  'صورة هوية الوالدين مفتوحة الملحق\n'
  'ملف الطالب التراكمي + شهادة نهاية العام الدراسي السابق بالعلامات\n'
  'تصديق شهادة الانتقال من مكتب التربية والتعليم الذي تتبع له المدرسة المنقولة منها الطالب',
  '',
  0,
  'full',
  'لا شيء محدد = تُطبع الوثائق الست كلها'),
 ('principal', 'اسم مدير المدرسة', 'Data', '', '', 0, 'half', '')]
# The documents a family is asked to bring, all of them unless the office
# unticks some (the design prints these when none is chosen).
DOCUMENTS = ['شهادة انتقال من المدرسة التي يدرس فيها الطالب',
 'صورة عن شهادة ميلاد الطالب',
 '6 صور شخصية',
 'صورة هوية الوالدين مفتوحة الملحق',
 'ملف الطالب التراكمي + شهادة نهاية العام الدراسي السابق بالعلامات',
 'تصديق شهادة الانتقال من مكتب التربية والتعليم الذي تتبع له المدرسة المنقولة منها الطالب']
