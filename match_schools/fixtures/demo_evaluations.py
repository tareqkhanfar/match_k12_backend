"""بيانات اختبار لنماذج التقييم — نماذج متنوّعة وتقييمات مملوءة.

كل ما تنشئه هذه الدالة يحمل الوسم أدناه في الوصف، ليتسنّى حذفه دفعة واحدة
دون لمس ما تكتبه المدرسة بنفسها.
"""
import random
import frappe
from frappe.utils import add_days, nowdate

TAG = "[بيانات اختبار]"

FORMS = [
	{
		"title": "سلوك الطالب في الصف",
		"form_type": "سلوك",
		"scale_type": "مقياس",
		"description": f"{TAG} تقييم دوري لسلوك الطالب داخل الحصة.",
		"scale": [
			{"label": "دائماً", "score": 3, "tone": "success"},
			{"label": "أحياناً", "score": 2, "tone": "warning"},
			{"label": "نادراً", "score": 1, "tone": "warning"},
			{"label": "أبداً", "score": 0, "tone": "danger"},
		],
		"criteria": [
			("الانضباط", "يستمع لزملائه دون مقاطعة"),
			("الانضباط", "يلتزم بمكانه في الصف"),
			("الانضباط", "لا يأكل أو يشرب أثناء الحصة"),
			("المشاركة", "يشارك في النقاش الصفي"),
			("المشاركة", "ينجز المهام الصفية في وقتها"),
			("التعامل", "يحترم زملاءه ومعلّميه"),
			("التعامل", "يتعاون في العمل الجماعي"),
			("الأدوات", "يُحضر أدواته ودفاتره"),
		],
	},
	{
		"title": "تقييم مهارات طفل الروضة — الفصل الثاني",
		"form_type": "روضة",
		"scale_type": "مقياس",
		"description": f"{TAG} نموذج مجالات لأطفال الروضة.",
		"scale": [
			{"label": "دائماً", "score": 3, "tone": "success"},
			{"label": "أحياناً", "score": 2, "tone": "warning"},
			{"label": "أبداً", "score": 0, "tone": "danger"},
		],
		"criteria": [
			("المظاهر الصحية العامة", "اهتمامه بالنظافة الشخصية"),
			("المظاهر الصحية العامة", "اهتمامه بالنظافة العامة للروضة"),
			("المظاهر الصحية العامة", "يغسل يديه قبل الطعام وبعده"),
			("المهارات الحياتية", "يرتدي الملابس ويخلعها بمفرده"),
			("المهارات الحياتية", "يرتّب أدواته في حقيبته"),
			("المهارات الحياتية", "يستخدم الحمّام دون مساعدة"),
			("المهارات الاجتماعية", "يشارك زملاءه اللعب"),
			("المهارات الاجتماعية", "يعبّر عن حاجته بالكلام"),
			("المهارات الحركية", "يمسك القلم بطريقة صحيحة"),
			("المهارات الحركية", "يقصّ بالمقص على خط مستقيم"),
		],
	},
	{
		"title": "تقرير تقدّم اللغة الإنجليزية — KG2",
		"form_type": "تقرير تقدم",
		"scale_type": "مقياس",
		"description": f"{TAG} تقرير تقدّم فصلي.",
		"scale": [
			{"label": "متميّز", "score": 4, "tone": "success"},
			{"label": "جيد", "score": 3, "tone": "info"},
			{"label": "مقبول", "score": 2, "tone": "warning"},
			{"label": "يحتاج دعماً", "score": 1, "tone": "danger"},
		],
		"criteria": [
			("Listening", "يفهم التعليمات البسيطة"),
			("Listening", "يميّز الأصوات الأولى للحروف"),
			("Speaking", "يعرّف عن نفسه بجملة كاملة"),
			("Speaking", "يسمّي الألوان والأرقام"),
			("Reading", "يتعرّف على الحروف الكبيرة والصغيرة"),
			("Writing", "يكتب اسمه بشكل صحيح"),
		],
	},
	{
		"title": "المهارات الحياتية والاعتماد على النفس",
		"form_type": "مهارات",
		"scale_type": "نعم/لا",
		"description": f"{TAG} قائمة تحقّق للمهارات الأساسية.",
		"scale": [
			{"label": "نعم", "score": 1, "tone": "success"},
			{"label": "لا", "score": 0, "tone": "danger"},
		],
		"criteria": [
			("", "ينظّم وقته بين الدراسة والأنشطة"),
			("", "يطلب المساعدة عند الحاجة"),
			("", "يتحمّل مسؤولية أدواته"),
			("", "يلتزم بمواعيد التسليم"),
			("", "يراجع عمله قبل تسليمه"),
		],
	},
	{
		"title": "تقييم المشاركة الصفية (من ١٠)",
		"form_type": "مشاركة صفية",
		"scale_type": "علامة رقمية",
		"description": f"{TAG} تقييم رقمي للمشاركة، لكل معيار علامة عظمى.",
		"scale": [],
		"criteria": [
			("", "المشاركة الشفوية", 10),
			("", "الالتزام بالواجبات", 10),
			("", "العمل الجماعي", 10),
			("", "الانضباط الصفي", 10),
		],
	},
]


def run(groups_limit=4, per_group=None):
	created_forms, created_entries = [], 0
	admin = "Administrator"
	frappe.set_user(admin)

	for spec in FORMS:
		# Matched on the tag, not on the title: a school's own form that happens
		# to share a name must not be skipped over or overwritten, and a
		# half-built stub left by a test must not block the real one.
		if frappe.db.exists(
			"MS Evaluation Form",
			{"title": spec["title"], "description": ["like", f"%{TAG}%"]},
		):
			print(f"  موجود مسبقاً: {spec['title']}")
			continue
		doc = frappe.new_doc("MS Evaluation Form")
		doc.title = spec["title"]
		doc.form_type = spec["form_type"]
		doc.scale_type = spec["scale_type"]
		doc.description = spec["description"]
		doc.is_active = 1
		doc.created_by_user = admin
		for i, opt in enumerate(spec["scale"]):
			doc.append("scale", {**opt, "sort_order": i})
		for i, row in enumerate(spec["criteria"]):
			category, item = row[0], row[1]
			maximum = row[2] if len(row) > 2 else 0
			doc.append(
				"criteria",
				{"category": category, "item": item, "max_score": maximum, "sort_order": i},
			)
		doc.insert(ignore_permissions=True)
		created_forms.append(doc.name)
		print(f"  ✓ {spec['title']} ({len(spec['criteria'])} بنداً)")

	frappe.db.commit()

	# تقييمات مملوءة لعدة شُعب، بتوزيع واقعي لا عشوائي بحت: أغلب الطلاب جيدون،
	# وقلّة تحتاج متابعة — وإلا بدت كل الشاشات وكأن الصف متطابق.
	forms = frappe.get_all(
		"MS Evaluation Form",
		filters={"description": ["like", f"%{TAG}%"]},
		fields=["name", "title", "scale_type"],
	)
	groups = frappe.get_all(
		"Student Group",
		filters={"disabled": 0},
		pluck="name",
		limit=groups_limit,
	)
	random.seed(20260830)

	for form in forms:
		doc = frappe.get_doc("MS Evaluation Form", form.name)
		options = [o.label for o in doc.scale]
		weights = [0.45, 0.35, 0.15, 0.05][: len(options)] or [1]
		for group in groups:
			roster = frappe.get_all(
				"Student Group Student",
				filters={"parent": group, "active": 1},
				fields=["student", "student_name"],
				limit=per_group or 100,
			)
			for row in roster:
				if frappe.db.exists(
					"MS Evaluation Entry", {"form": form.name, "student": row.student}
				):
					continue
				entry = frappe.new_doc("MS Evaluation Entry")
				entry.form = form.name
				entry.form_title = doc.title
				entry.student = row.student
				entry.student_name = row.student_name
				entry.student_group = group
				entry.evaluated_by = admin
				entry.evaluated_on = frappe.utils.now()
				entry.is_published = 1
				total = maximum = 0.0
				for c in doc.criteria:
					if doc.scale_type == "علامة رقمية":
						ceiling = c.max_score or 10
						score = round(random.triangular(ceiling * 0.5, ceiling, ceiling * 0.85))
						label = str(score)
					else:
						label = random.choices(options, weights=weights[: len(options)])[0]
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
				entry.total_score = total
				entry.max_score = maximum
				entry.percent = round(total * 100 / maximum, 2) if maximum else 0
				entry.insert(ignore_permissions=True)
				created_entries += 1
		frappe.db.commit()
		print(f"  ✓ تقييمات {form.title}")

	print(f"\nالنتيجة: {len(created_forms)} نموذجاً جديداً، {created_entries} تقييماً")
	print(f"كلها موسومة بـ {TAG} في الوصف — للحذف لاحقاً.")


def clear():
	"""حذف كل ما أنشأته هذه البيانات، دون لمس ما كتبته المدرسة."""
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
