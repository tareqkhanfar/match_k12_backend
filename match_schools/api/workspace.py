"""مساحة العمل — الأرقام التي تجعل الاختصار يستحق الضغط.

اختصار بلا رقم مجرّد رابط ثانٍ إلى ما في القائمة الجانبية أصلاً. الرقم هو ما
يغيّر السلوك: «٣٩ فاتورة غير مدفوعة» تُقرأ فتُفتح، و«الفواتير» تُقرأ فتُترك.

كل عدّاد يُحسب ضمن نطاق من يسأل — المعلّم يرى طلاب شُعبه لا طلاب المدرسة —
ويُحسب بـ COUNT لا بجلب الصفوف، لأن هذه الشاشة أول ما يُفتح بعد الدخول ولا
يجوز أن تكون أبطأ ما فيه.
"""

import json

import frappe
from frappe.utils import nowdate

from match_schools.api.utils import (
	fail,
	get_default_academic_year,
	get_persona,
	instructor_groups,
	ms_endpoint,
	parse_json_arg,
	PERSONA_LABELS_AR,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (*BACK_OFFICE, ROLE_TEACHER)
ALL_ROLES = (*STAFF, ROLE_STUDENT, ROLE_PARENT)


def _safe(fn, default=0):
	"""عدّاد واحد لا يجوز أن يُسقط الشاشة كلها."""
	try:
		return fn()
	except Exception:
		return default


def _my_groups(persona: str) -> list[str] | None:
	if persona in BACK_OFFICE:
		return None
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		# مصدر واحد لكل الشاشات، ومقيَّد بالفصل المختار.
		return sorted(
			set(instructor_groups(scope.get("instructor")))
			| set(scope.get("student_groups") or [])
		)
	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", scope.get("students") or [""]], "active": 1},
				fields=["parent"],
			)
		}
	)


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def shortcuts(persona: str = None):
	"""الأرقام الحيّة خلف اختصارات مساحة العمل."""
	scope = resolve_scope(persona)
	groups = _my_groups(persona)
	year = get_default_academic_year()
	out: list[dict] = []

	def add(key, label, count, hint, route, tone="muted", search=None):
		out.append(
			{
				"key": key,
				"label": label,
				"count": count,
				"hint": hint,
				"route": route,
				"tone": tone,
				"search": search or {},
			}
		)

	if persona in BACK_OFFICE:
		add(
			"students", "الطلاب",
			_safe(lambda: frappe.db.count("Student", {"enabled": 1})),
			"نشط", "/app/students",
		)
		add(
			"instructors", "المعلمون",
			_safe(lambda: frappe.db.count("Instructor")),
			"", "/app/teachers",
		)
		add(
			"programs", "الصفوف",
			_safe(lambda: frappe.db.count("Program")),
			"", "/app/classes",
		)
		add(
			"courses", "المواد",
			_safe(lambda: frappe.db.count("Course")),
			"", "/app/subjects",
		)
		add(
			"unpaid", "الفواتير",
			_safe(
				lambda: frappe.db.count(
					"Sales Invoice", {"docstatus": 1, "status": ["in", ["Unpaid", "Overdue"]]}
				)
			),
			"غير مدفوعة", "/app/fees", "danger",
		)
		add(
			"admissions", "طلبات الالتحاق",
			_safe(
				lambda: frappe.db.count("Student Applicant", {"application_status": "Applied"})
			),
			"قيد الدراسة", "/app/admissions", "warning",
		)
		add(
			"print", "طلبات الطباعة",
			_safe(lambda: frappe.db.count("MS Print Request", {"status": "Submitted"})),
			"بانتظار الطباعة", "/app/print-requests", "warning",
		)

	if persona == ROLE_TEACHER:
		add(
			"my_students", "طلابي",
			_safe(
				lambda: frappe.db.count(
					"Student Group Student",
					{"parent": ["in", groups or [""]], "active": 1},
				)
			),
			"في شُعبي", "/app/students",
		)
		add(
			"my_classes", "شُعبي",
			len(groups or []), "", "/app/classes",
		)
		add(
			"to_grade", "واجبات للتصحيح",
			_safe(
				lambda: frappe.db.count(
					"MS Assignment Submission",
					{"status": "Submitted", "assignment": ["in", _my_assignments(scope) or [""]]},
				)
			),
			"سُلّمت ولم تُصحّح", "/app/assignments", "warning",
		)
		add(
			"print", "طلبات طباعتي",
			_safe(
				lambda: frappe.db.count(
					"MS Print Request",
					{"requested_by": frappe.session.user, "status": ["!=", "Collected"]},
				)
			),
			"قيد المعالجة", "/app/print-requests",
		)

	if persona in STAFF:
		add(
			"attendance_today", "حضور اليوم",
			_safe(
				lambda: frappe.db.count(
					"Student Attendance",
					{
						"date": nowdate(),
						"docstatus": 1,
						**({"student_group": ["in", groups]} if groups else {}),
					},
				)
			),
			"سجل مرصود", "/app/attendance",
		)
		add(
			"appointments", "مواعيد بانتظارك",
			_safe(
				lambda: frappe.db.count(
					"MS Appointment", {"staff_user": frappe.session.user, "status": "Requested"}
				)
			),
			"طلب", "/app/appointments", "warning",
		)

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		students = scope.get("students") or []
		add(
			"assignments", "واجبات مستحقّة",
			_safe(lambda: _due_assignments(groups, students)),
			"هذا الأسبوع", "/app/assignments", "warning",
		)
		add(
			"class_log", "دفتر الحصص",
			_safe(
				lambda: frappe.db.count(
					"MS Class Log",
					{"student_group": ["in", groups or [""]], "is_published": 1},
				)
			),
			"حصة موثّقة", "/app/class-log",
		)
		add(
			"evaluations", "تقييماتي",
			_safe(
				lambda: frappe.db.count(
					"MS Evaluation Entry",
					{"student": ["in", students or [""]], "is_published": 1},
				)
			),
			"منشور", "/app/behaviour",
		)
		add(
			"appointments", "مواعيدي",
			_safe(
				lambda: frappe.db.count(
					"MS Appointment",
					{
						"requested_by": frappe.session.user,
						"status": ["in", ["Requested", "Confirmed"]],
					},
				)
			),
			"قائم", "/app/appointments",
		)

	# للجميع: بريد غير مقروء — الرقم الوحيد الذي يعني "افعل شيئاً الآن".
	add(
		"unread_mail", "المراسلات",
		_safe(
			lambda: frappe.db.count(
				"MS Message Recipient",
				{
					"user": frappe.session.user,
					"is_read": 0,
					"is_archived": 0,
					"is_deleted": 0,
					"is_pending": 0,
				},
			)
		),
		"غير مقروءة", "/app/mail", "primary",
	)

	return {"shortcuts": out, "academic_year": year}


def _my_assignments(scope: dict) -> list[str]:
	instructor = scope.get("instructor")
	if not instructor:
		return []
	return frappe.get_all("MS Assignment", filters={"instructor": instructor}, pluck="name")


def _due_assignments(groups: list[str] | None, students: list[str]) -> int:
	from frappe.utils import add_days, today

	from match_schools.api.assignments import assignments_for_groups, handed_in_pairs

	rows = assignments_for_groups(
		groups or [],
		{"status": "Open", "due_date": ["between", [today(), add_days(today(), 7)]]},
	)
	if not rows:
		return 0
	done = handed_in_pairs([r.name for r in rows])
	return sum(1 for r in rows for s in students if (r.name, s) not in done)


# --- Per-user layout ----------------------------------------------------------
#
# The workspace is the same for everyone in a role, which is right until it
# isn't: a school hands one secretary the admissions work and another the fees,
# and both stare past half the screen. A layout says what one person sees —
# which sections, which links inside them, and which of the counted cards —
# and everyone without one keeps the default.

def _layout_of(user: str) -> dict:
	stored = frappe.db.get_value("MS Workspace Layout", {"user": user}, "layout")
	if not stored:
		return {}
	try:
		return json.loads(stored) or {}
	except Exception:
		# A layout that cannot be read must not take the workspace with it.
		return {}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def my_layout(persona: str = None):
	"""What this user's workspace should show. Empty means the default."""
	return {"layout": _layout_of(frappe.session.user), "user": frappe.session.user}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def layout_users(search: str = "", persona: str = None):
	"""Who can be customised, and who already is."""
	filters = {"enabled": 1, "name": ["not in", ["Guest"]]}
	or_filters = (
		[["full_name", "like", f"%{search}%"], ["name", "like", f"%{search}%"]] if search else None
	)
	rows = frappe.get_all(
		"User",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "full_name", "user_type"],
		order_by="full_name",
		limit=60,
	)
	customised = set(frappe.get_all("MS Workspace Layout", pluck="user"))
	out = []
	for r in rows:
		user_persona = get_persona(r.name)
		if not user_persona:
			continue
		out.append(
			{
				"user": r.name,
				"name": r.full_name or r.name,
				"persona": user_persona,
				"personaLabel": PERSONA_LABELS_AR.get(user_persona, user_persona),
				"customised": r.name in customised,
			}
		)
	return {"users": out}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def layout_for(user: str, persona: str = None):
	"""One user's layout, with the persona whose default it departs from."""
	target = get_persona(user)
	if not target:
		return fail(
			"This user has no Match Schools role.",
			"هذا المستخدم بلا دور في النظام — لا مساحة عمل له.",
		)
	return {
		"user": user,
		"persona": target,
		"personaLabel": PERSONA_LABELS_AR.get(target, target),
		"layout": _layout_of(user),
		"cards": [{"key": c["key"], "label": c["label"]} for c in _cards_for(target)],
	}


def _cards_for(target_persona: str) -> list[dict]:
	"""The counted cards a persona's workspace offers.

	Built by the same code that builds the real ones, so a card added there
	appears here without being listed twice. The counts belong to whoever is
	asking and are discarded — only the keys and labels are wanted.
	"""
	# Down to the function itself: the wrappers above it replace the persona
	# argument with the caller's own, which would hand an admin's cards back
	# for every user being customised.
	builder = shortcuts
	while hasattr(builder, "__wrapped__"):
		builder = builder.__wrapped__
	try:
		return (builder(persona=target_persona) or {}).get("shortcuts") or []
	except Exception:
		frappe.clear_messages()
		return []


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_layout(user: str, layout: str | dict = None, persona: str = None):
	"""Store one user's workspace, or clear it back to the default."""
	if not get_persona(user):
		return fail(
			"This user has no Match Schools role.",
			"هذا المستخدم بلا دور في النظام — لا مساحة عمل له.",
		)
	data = parse_json_arg(layout, {}) or {}
	empty = not any(
		data.get(key) for key in ("hiddenGroups", "hiddenItems", "hiddenCards", "groupOrder")
	)
	existing = frappe.db.get_value("MS Workspace Layout", {"user": user}, "name")
	if empty:
		if existing:
			frappe.delete_doc("MS Workspace Layout", existing, ignore_permissions=True)
			frappe.db.commit()
		return {"user": user, "layout": {}, "reset": True}

	doc = (
		frappe.get_doc("MS Workspace Layout", existing)
		if existing
		else frappe.new_doc("MS Workspace Layout")
	)
	doc.user = user
	doc.updated_by_user = frappe.session.user
	doc.layout = json.dumps(data, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"user": user, "layout": data, "reset": False}
