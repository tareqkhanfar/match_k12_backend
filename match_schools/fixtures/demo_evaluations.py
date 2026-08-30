"""بيانات تجريبية لنماذج التقييم — قريبة من الواقع لا عشوائية.

الغرض أن تبدو الشاشات كما ستبدو في مدرسة تعمل، لا كضجيج ملوّن. أهم ما يفرّق
بين الاثنين شيء واحد: **الطالب له شخصية ثابتة**. الطالب المجتهد يميل إلى
"دائماً" في أغلب البنود، والمتعثّر إلى "أحياناً" و"أبداً" — مع تفاوت، لأن أحداً
ليس متسقاً تماماً. لو رُسمت كل إجابة عشوائياً ومستقلة عن أختها لتكدّس الجميع
حول المتوسط، ولبدت الشاشة صحيحة وهي تقول لا شيء.

وواقعية أخرى تُنسى عادةً: المعلّم لا يُنهي الصف دائماً، ولا يُظهر كل تقييم
فور كتابته، ولا يكتب ملاحظة لكل طالب — إنما للحالات اللافتة. كل ذلك هنا.

كل ما تنشئه `run()` موسوم في الوصف، و`clear()` يزيله وحده دون لمس ما تكتبه
المدرسة بنفسها.
"""

import random

import frappe
from frappe.utils import add_days, nowdate

TAG = "[بيانات تجريبية]"

# كل نموذج: تعريفه، ولمن يُستخدم، وكم نسبة الصف التي أنجزها المعلّم فعلاً.
FORMS = [
	{
		"key": "behaviour",
		"ability": (0.8, 0.16),
		"title": "سلوك الطالب في الصف",
		"form_type": "سلوك",
		"scale_type": "مقياس",
		"description": "تقييم دوري لسلوك الطالب داخل الحصة، يُملأ في نهاية كل شهر.",
		"scale": [
			("دائماً", 3, "success"),
			("غالباً", 2, "info"),
			("أحياناً", 1, "warning"),
			("نادراً", 0, "danger"),
		],
		"criteria": [
			("الانضباط", "يحضر إلى الحصة في وقتها"),
			("الانضباط", "يستمع دون مقاطعة زملائه"),
			("الانضباط", "يلتزم بمكانه وبتعليمات المعلّم"),
			("الانضباط", "يمتنع عن الأكل والشرب أثناء الحصة"),
			("المشاركة", "يشارك في النقاش الصفي"),
			("المشاركة", "يجيب عن الأسئلة الموجّهة له"),
			("المشاركة", "ينجز المهام الصفية في وقتها"),
			("التعامل", "يحترم زملاءه ومعلّميه"),
			("التعامل", "يتعاون في العمل الجماعي"),
			("الأدوات", "يُحضر كتبه ودفاتره وأدواته"),
			("الأدوات", "يحافظ على نظافة مكانه"),
		],
		"grades": None,          # كل الصفوف
		"coverage": 0.92,
		"published": 0.85,
	},
	{
		"key": "life_skills",
		"ability": (0.84, 0.17),
		"title": "المهارات الحياتية والاعتماد على النفس",
		"form_type": "مهارات",
		"scale_type": "نعم/لا",
		"description": "قائمة تحقّق للصفوف الأولى، تُراجع مرتين في الفصل.",
		"scale": [("نعم", 1, "success"), ("لا", 0, "danger")],
		"criteria": [
			("", "يرتّب حقيبته وأدواته بنفسه"),
			("", "يذهب إلى الحمّام ويعود دون مساعدة"),
			("", "يفتح علبة طعامه ويأكل بمفرده"),
			("", "يربط حذاءه ويرتدي معطفه"),
			("", "يطلب المساعدة عند الحاجة"),
			("", "يتذكّر إحضار واجباته"),
		],
		"grades": ["الصف الأول", "الصف الثاني", "الصف الثالث"],
		"coverage": 0.95,
		"published": 0.9,
	},
	{
		"key": "domains",
		"ability": (0.78, 0.17),
		"title": "تقييم المجالات النمائية — الصفوف الأولى",
		"form_type": "روضة",
		"scale_type": "مقياس",
		"description": "نموذج المجالات: صحية وحركية واجتماعية ولغوية.",
		"scale": [("دائماً", 3, "success"), ("أحياناً", 2, "warning"), ("أبداً", 0, "danger")],
		"criteria": [
			("المظاهر الصحية العامة", "يهتم بنظافته الشخصية"),
			("المظاهر الصحية العامة", "يغسل يديه قبل الطعام وبعده"),
			("المظاهر الصحية العامة", "يحافظ على نظافة الصف"),
			("المهارات الحركية", "يمسك القلم بطريقة صحيحة"),
			("المهارات الحركية", "يقصّ بالمقص على خط مستقيم"),
			("المهارات الحركية", "يتحكّم بحركته أثناء اللعب"),
			("المهارات الاجتماعية", "يشارك زملاءه اللعب والأدوات"),
			("المهارات الاجتماعية", "ينتظر دوره"),
			("المهارات الاجتماعية", "يعبّر عن حاجته بالكلام"),
			("المهارات اللغوية", "يتحدّث بجمل مفيدة"),
			("المهارات اللغوية", "يصغي للقصة حتى نهايتها"),
		],
		"grades": ["الصف الأول", "الصف الثاني"],
		"coverage": 0.9,
		"published": 0.8,
	},
	{
		"key": "english",
		"ability": (0.7, 0.19),
		"title": "تقرير تقدّم اللغة الإنجليزية",
		"form_type": "تقرير تقدم",
		"scale_type": "مقياس",
		"description": "تقرير فصلي يُرفق بكشف العلامات.",
		"scale": [
			("متميّز", 4, "success"),
			("جيد", 3, "info"),
			("مقبول", 2, "warning"),
			("يحتاج دعماً", 1, "danger"),
		],
		"criteria": [
			("Listening", "يفهم التعليمات الصفية البسيطة"),
			("Listening", "يميّز أصوات الحروف"),
			("Speaking", "يعرّف عن نفسه بجمل كاملة"),
			("Speaking", "يستخدم المفردات الجديدة في جملة"),
			("Reading", "يقرأ الكلمات البصرية المقرّرة"),
			("Reading", "يفهم النص القصير ويجيب عنه"),
			("Writing", "يكتب الحروف بخطّ واضح"),
			("Writing", "يكتب جملة صحيحة إملائياً"),
		],
		"grades": ["الصف الثالث", "الصف الرابع", "الصف الخامس", "الصف السادس"],
		"coverage": 0.88,
		"published": 0.75,
	},
	{
		"key": "participation",
		"ability": (0.75, 0.17),
		"title": "المشاركة والانضباط الصفي (من ١٠)",
		"form_type": "مشاركة صفية",
		"scale_type": "علامة رقمية",
		"description": "تقدير رقمي شهري يدخل ضمن علامة المشاركة.",
		"scale": [],
		"criteria": [
			("", "المشاركة الشفوية", 10),
			("", "الالتزام بالواجبات", 10),
			("", "العمل ضمن مجموعة", 10),
			("", "الانضباط والهدوء", 10),
		],
		"grades": [
			"الصف السابع", "الصف الثامن", "الصف التاسع",
			"الصف العاشر", "الصف الحادي عشر", "الصف الثاني عشر",
		],
		"coverage": 0.94,
		"published": 0.9,
	},
	{
		"key": "support",
		"ability": (0.52, 0.18),
		"title": "تقييم الأداء للطلبة ذوي الاحتياجات الخاصة",
		"form_type": "مهارات",
		"scale_type": "مقياس",
		"description": "متابعة فردية لخطة الدعم، تُملأ لعدد محدود من الطلبة.",
		"scale": [
			("مستقل", 3, "success"),
			("بمساعدة بسيطة", 2, "info"),
			("بمساعدة كاملة", 1, "warning"),
			("لم يتحقّق", 0, "danger"),
		],
		"criteria": [
			("الأكاديمي", "يتابع المهمة حتى إنهائها"),
			("الأكاديمي", "ينسخ من السبورة"),
			("الأكاديمي", "يستجيب للتعليمات المكتوبة"),
			("السلوكي", "يجلس في مكانه طوال الحصة"),
			("السلوكي", "ينتقل بين الأنشطة دون توتّر"),
			("التواصلي", "يطلب حاجته بوضوح"),
			("التواصلي", "يبادر بالتواصل مع زميل"),
		],
		"grades": None,
		"coverage": 0.2,      # قلّة من الطلبة، كما هو الحال فعلاً
		"published": 0.6,
	},
]

# ملاحظات المعلّمين — تُكتب للحالات اللافتة لا للجميع، إيجاباً وسلباً.
NOTES_GOOD = [
	"تحسّن ملحوظ هذا الشهر، وأصبح يبادر بالمشاركة.",
	"مثال جيد لزملائه في الالتزام والتعاون.",
	"يُنجز مهامه قبل الوقت ويساعد من حوله.",
	"انتقل من التردّد إلى الثقة في العرض أمام الصف.",
]
NOTES_WATCH = [
	"يحتاج إلى تذكير متكرّر بإحضار أدواته.",
	"يشتّت انتباهه بسرعة — جُلس في الصف الأمامي.",
	"تمّ التواصل مع وليّ الأمر بخصوص الالتزام بالواجبات.",
	"يتحسّن عند تكليفه بدور محدّد داخل المجموعة.",
]


def _groups_for(grades):
	"""الشُعب التي فيها طلاب فعلاً، مقيّدة بالصفوف المطلوبة إن وُجدت."""
	rows = frappe.db.sql(
		"""select sgs.parent as name, sg.program, count(*) as n
		     from `tabStudent Group Student` sgs
		     join `tabStudent Group` sg on sg.name = sgs.parent
		    where sgs.active = 1 and ifnull(sg.disabled, 0) = 0
		    group by sgs.parent, sg.program having n > 0""",
		as_dict=True,
	)
	if grades:
		rows = [r for r in rows if r.program in grades]
	return [r.name for r in rows]


def _ability(rng, spec):
	"""ميل الطالب العام في هذا النموذج تحديداً.

	لكل نموذج مركزه: أغلب الصف سلوكه جيد، بينما خطة الدعم تُفتح أصلاً للطلبة
	المتعثّرين فتتوزّع أدنى. مركز واحد لكل النماذج يجعل الشاشات كلها تقول
	الشيء نفسه، وهو ما لا يحدث في مدرسة.
	"""
	mean, sd = spec.get("ability", (0.72, 0.18))
	return min(max(rng.gauss(mean, sd), 0.05), 0.99)


def _pick(rng, options, ability):
	"""اختيار خيار يتناسب مع ميل الطالب.

	الخيارات مرتّبة من الأفضل إلى الأسوأ، فالقيمة المرجوّة هي موقع الطالب على
	المقياس، ثم تُزاح قليلاً — لأن أحداً ليس متسقاً في كل بند.
	"""
	span = len(options) - 1
	target = (1 - ability) * span            # 0 = الأفضل
	# التشويش متناسب مع طول المقياس. قيمة ثابتة تصلح لمقياس من أربعة خيارات
	# تمسح الميل تماماً في مقياس "نعم/لا"، فيخرج نصف الصف "نعم" ونصفه "لا"
	# مهما كان الطالب — وهو ما لا يحدث في قائمة تحقّق لصفوف أولى.
	idx = round(min(max(rng.gauss(target, 0.25 + 0.2 * span), 0), span))
	return options[int(idx)]


def run(max_groups_per_form=4, seed=20260830):
	frappe.set_user("Administrator")
	rng = random.Random(seed)
	made_forms, made_entries = 0, 0

	for spec in FORMS:
		existing = frappe.db.get_value(
			"MS Evaluation Form",
			{"title": spec["title"], "description": ["like", f"%{TAG}%"]},
			"name",
		)
		if existing:
			doc = frappe.get_doc("MS Evaluation Form", existing)
		else:
			doc = frappe.new_doc("MS Evaluation Form")
			doc.title = spec["title"]
			doc.form_type = spec["form_type"]
			doc.scale_type = spec["scale_type"]
			doc.description = f"{spec['description']} {TAG}"
			doc.is_active = 1
			doc.allow_notes = 1
			doc.created_by_user = "Administrator"
			for i, (label, score, tone) in enumerate(spec["scale"]):
				doc.append("scale", {"label": label, "score": score, "tone": tone, "sort_order": i})
			for i, row in enumerate(spec["criteria"]):
				doc.append(
					"criteria",
					{
						"category": row[0],
						"item": row[1],
						"max_score": row[2] if len(row) > 2 else 0,
						"sort_order": i,
					},
				)
			doc.insert(ignore_permissions=True)
			made_forms += 1
			print(f"  ✓ {spec['title']} ({len(spec['criteria'])} بنداً)")

		groups = _groups_for(spec["grades"])
		rng.shuffle(groups)
		groups = groups[:max_groups_per_form]
		options = [o.label for o in doc.scale]
		numeric = doc.scale_type == "علامة رقمية"

		for group in groups:
			roster = frappe.get_all(
				"Student Group Student",
				filters={"parent": group, "active": 1},
				fields=["student", "student_name"],
			)
			for row in roster:
				# المعلّم لم يُنهِ الصف كلّه — وهذا هو الحال عادةً.
				if rng.random() > spec["coverage"]:
					continue
				if frappe.db.exists(
					"MS Evaluation Entry", {"form": doc.name, "student": row.student}
				):
					continue

				ability = _ability(rng, spec)
				entry = frappe.new_doc("MS Evaluation Entry")
				entry.form = doc.name
				entry.form_title = doc.title
				entry.student = row.student
				entry.student_name = row.student_name
				entry.student_group = group
				entry.evaluated_by = "Administrator"
				# تواريخ موزّعة على الأسابيع الماضية، لا كلها اليوم نفسه.
				entry.evaluated_on = f"{add_days(nowdate(), -rng.randint(1, 45))} 10:{rng.randint(0, 59):02d}:00"
				entry.is_published = 1 if rng.random() < spec["published"] else 0

				total = maximum = 0.0
				for c in doc.criteria:
					if numeric:
						ceiling = c.max_score or 10
						score = round(min(max(rng.gauss(ability * ceiling, ceiling * 0.12), 0), ceiling))
						label = str(score)
					else:
						label = _pick(rng, options, ability)
						score = next((o.score for o in doc.scale if o.label == label), 0)
						ceiling = max((o.score for o in doc.scale), default=0)
					total += score
					maximum += ceiling
					entry.append(
						"answers",
						{
							"criterion_key": c.name,
							"category": c.category,
							"item": c.item,
							"value_label": label,
							"score": score,
						},
					)

				percent = round(total * 100 / maximum, 2) if maximum else 0
				# ملاحظة للحالات اللافتة فقط، كما يفعل معلّم حقيقي.
				if percent >= 88 and rng.random() < 0.5:
					entry.notes = rng.choice(NOTES_GOOD)
				elif percent <= 55 and rng.random() < 0.7:
					entry.notes = rng.choice(NOTES_WATCH)

				entry.total_score = total
				entry.max_score = maximum
				entry.percent = percent
				entry.insert(ignore_permissions=True)
				made_entries += 1

			frappe.db.commit()

	print(f"\nالنتيجة: {made_forms} نموذجاً جديداً، {made_entries} تقييماً")
	print(f"كلها موسومة بـ {TAG} — لحذفها: fixtures.demo_evaluations.clear()")


def clear():
	"""حذف ما أنشأته هذه البيانات وحدها، بالوسم لا بالاسم."""
	forms = frappe.get_all(
		"MS Evaluation Form", filters={"description": ["like", f"%{TAG}%"]}, pluck="name"
	)
	entries = frappe.get_all(
		"MS Evaluation Entry", filters={"form": ["in", forms or [""]]}, pluck="name"
	)
	for n in entries:
		frappe.delete_doc("MS Evaluation Entry", n, ignore_permissions=True, force=True)
	for n in forms:
		frappe.delete_doc("MS Evaluation Form", n, ignore_permissions=True, force=True)
	frappe.db.commit()
	print(f"حُذف {len(forms)} نموذجاً و{len(entries)} تقييماً")
