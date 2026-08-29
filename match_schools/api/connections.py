"""Everything you can do from inside one class.

A teacher standing on their 4-B page wants to set an exam for 4-B, mail 4-B's
guardians, mark 4-B's register. Today each of those means leaving the class,
opening another screen, and finding 4-B again in a dropdown — three steps to
get back where they started.

This lists the connections a class has, with how many of each already exist
and where each one opens with the class already chosen. The counts are what
make it worth reading: "الواجبات ٣" tells a teacher something, "الواجبات" does
not.

Nothing here decides permissions. Every target screen enforces its own, and a
row this returns is a link, not an authorisation — it is filtered by persona
purely so a student is not shown a door that will be shut in their face.
"""

import frappe
from frappe import _
from frappe.utils import cint, nowdate

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	resolve_scope,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
ALL_ROLES = (*STAFF, ROLE_STUDENT, ROLE_PARENT)

# One row per thing a class connects to.
#
#   key      what the screen keys the row by
#   label    Arabic, because that is what the sidebar says
#   icon     a lucide name the frontend already imports
#   route    where it opens, with `?group=` appended by the caller
#   doctype  what to count, and the field holding the class
#   roles    who is even shown the row
#   action   True for "create something new here" rather than "go and look"
CONNECTIONS = [
	{
		"key": "students", "label": "طلاب الشعبة", "icon": "Users",
		"route": "/app/students", "group": "الشعبة",
		"doctype": None, "roles": STAFF,
	},
	{
		"key": "timetable", "label": "الجدول الدراسي", "icon": "CalendarDays",
		"route": "/app/timetable", "group": "الشعبة",
		"doctype": "MS Timetable Slot", "field": "student_group", "roles": ALL_ROLES,
	},
	{
		"key": "attendance", "label": "الحضور والغياب", "icon": "ClipboardCheck",
		"route": "/app/attendance", "group": "المتابعة اليومية",
		"doctype": None, "roles": STAFF,
	},
	{
		"key": "gradebook", "label": "رصد العلامات", "icon": "BookOpenCheck",
		"route": "/app/gradebook", "group": "العلامات",
		"doctype": None, "roles": STAFF,
	},
	{
		"key": "record", "label": "علامات الطلبة", "icon": "Award",
		"route": "/app/record", "group": "العلامات",
		"doctype": None, "roles": ALL_ROLES,
	},
	{
		"key": "finals", "label": "العلامات النهائية", "icon": "Award",
		"route": "/app/finals", "group": "العلامات",
		"doctype": None, "roles": ALL_ROLES,
	},
	{
		"key": "assessment-plan", "label": "خطة التقييم", "icon": "Layers",
		"route": "/app/assessment-plan", "group": "العلامات",
		"doctype": None, "roles": STAFF,
	},
	{
		"key": "quarter-results", "label": "شهادات الأرباع", "icon": "FileText",
		"route": "/app/quarter-results", "group": "العلامات",
		"doctype": None, "roles": STAFF,
	},
	{
		"key": "exams", "label": "الامتحانات", "icon": "FileSpreadsheet",
		"route": "/app/exams", "group": "التدريس",
		"doctype": "Assessment Plan", "field": "student_group", "roles": ALL_ROLES,
		"action": True,
	},
	{
		"key": "assignments", "label": "الواجبات", "icon": "NotebookPen",
		"route": "/app/assignments", "group": "التدريس",
		"doctype": "MS Assignment", "field": "student_group", "roles": ALL_ROLES,
		"action": True,
	},
	{
		"key": "quizzes", "label": "الاختبارات الإلكترونية", "icon": "FileQuestion",
		"route": "/app/quizzes", "group": "التدريس",
		"doctype": "MS Quiz", "field": "student_group", "roles": ALL_ROLES,
		"action": True,
	},
	{
		"key": "resources", "label": "مصادر المواد", "icon": "BookMarked",
		"route": "/app/resources", "group": "التدريس",
		"doctype": "MS Resource", "field": "student_group", "roles": ALL_ROLES,
		"action": True,
	},
	{
		"key": "files", "label": "نشر ملف للشعبة", "icon": "FolderOpen",
		"route": "/app/files", "group": "التدريس",
		"doctype": "MS Drive File", "field": "student_group", "roles": STAFF,
		"action": True,
	},
	{
		"key": "print-requests", "label": "طلب طباعة", "icon": "Printer",
		"route": "/app/print-requests", "group": "التدريس",
		"doctype": "MS Print Request", "field": "student_group", "roles": STAFF,
		"action": True,
	},
	{
		"key": "mail", "label": "مراسلة الشعبة", "icon": "Mail",
		"route": "/app/mail", "group": "التواصل",
		"doctype": None, "roles": STAFF, "action": True,
	},
	{
		"key": "community", "label": "منشور للشعبة", "icon": "Users2",
		"route": "/app/community", "group": "التواصل",
		"doctype": "MS Community Post", "field": "student_group", "roles": STAFF,
		"action": True,
	},
	{
		"key": "gallery", "label": "معرض الصور", "icon": "Images",
		"route": "/app/classes", "group": "التواصل",
		"doctype": "MS Gallery Album", "field": "student_group", "roles": ALL_ROLES,
		"action": True,
	},
	{
		"key": "surveys", "label": "الاستبيانات", "icon": "ClipboardList",
		"route": "/app/surveys", "group": "التواصل",
		"doctype": "MS Survey", "field": "student_group", "roles": ALL_ROLES,
		"action": True,
	},
	{
		"key": "behaviour", "label": "السلوك والانضباط", "icon": "ShieldAlert",
		"route": "/app/behaviour", "group": "المتابعة اليومية",
		"doctype": "MS Behaviour Record", "field": "student_group", "roles": STAFF,
		"action": True,
	},
	{
		"key": "activities", "label": "الأنشطة والرحلات", "icon": "Ticket",
		"route": "/app/activities", "group": "المتابعة اليومية",
		"doctype": "MS Activity", "field": "student_group", "roles": ALL_ROLES,
	},
	{
		"key": "reports", "label": "تقارير الشعبة", "icon": "BarChart3",
		"route": "/app/reports", "group": "المتابعة اليومية",
		"doctype": None, "roles": STAFF,
	},
]


def _may_see_group(persona: str, student_group: str) -> bool:
	"""Whether this caller has any business looking at this class."""
	if persona in BACK_OFFICE:
		return True

	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		mine = set(scope.get("student_groups") or [])
		if student_group in mine:
			return True
		instructor = scope.get("instructor")
		return bool(
			instructor
			and frappe.db.exists(
				"MS Timetable Slot",
				{"student_group": student_group, "instructor": instructor, "active": 1},
			)
		)

	students = scope.get("students") or []
	return bool(
		students
		and frappe.db.exists(
			"Student Group Student",
			{"parent": student_group, "student": ["in", students], "active": 1},
		)
	)


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def for_class(student_group: str = None, persona: str = None):
	"""The connections one class has, with counts and where each opens."""
	if not student_group:
		return fail(message_en="A class is required.", message_ar="يجب تحديد الشعبة.")
	if not frappe.db.exists("Student Group", student_group):
		return fail(message_en="Class not found.", message_ar="الشعبة غير موجودة.")
	if not _may_see_group(persona, student_group):
		frappe.throw(_("This class is not yours."), frappe.PermissionError)

	year = get_default_academic_year()
	term = get_default_academic_term()

	rows = []
	for spec in CONNECTIONS:
		if persona not in spec["roles"]:
			continue

		count = None
		doctype = spec.get("doctype")
		if doctype and frappe.db.table_exists(doctype):
			filters = {spec["field"]: student_group}
			meta = frappe.get_meta(doctype)
			# Counted inside the selected period, so the number next to a row
			# means "this term" and not "since the school opened".
			if year and meta.has_field("academic_year"):
				filters["academic_year"] = year
			if term and meta.has_field("academic_term"):
				filters["academic_term"] = ["in", [term, "", None]]
			try:
				count = frappe.db.count(doctype, filters)
			except Exception:
				count = None

		rows.append(
			{
				"key": spec["key"],
				"label": spec["label"],
				"icon": spec["icon"],
				"group": spec["group"],
				"route": spec["route"],
				"count": count,
				"action": bool(spec.get("action")),
			}
		)

	info = frappe.db.get_value(
		"Student Group",
		student_group,
		["student_group_name", "program", "batch"],
		as_dict=True,
	) or {}

	courses = _courses_of(student_group, persona)

	return {
		"student_group": student_group,
		"name": info.get("student_group_name"),
		"program": info.get("program"),
		"batch": info.get("batch"),
		"students": frappe.db.count(
			"Student Group Student", {"parent": student_group, "active": 1}
		),
		"courses": courses,
		"connections": rows,
		# Ordered here rather than in the screen so every place that renders a
		# connections panel groups them the same way.
		"groups": ["الشعبة", "التدريس", "العلامات", "المتابعة اليومية", "التواصل"],
	}


def _courses_of(student_group: str, persona: str) -> list[dict]:
	"""Subjects of this class the caller may act on.

	A teacher gets only what they themselves teach: the connections panel
	links straight into mark entry, and offering a colleague's subject there
	is the same leak the gradebook already closed.
	"""
	filters = {"student_group": student_group, "active": 1}
	if persona == ROLE_TEACHER:
		instructor = resolve_scope(persona).get("instructor")
		if not instructor:
			return []
		filters["instructor"] = instructor

	names = {
		r.course
		for r in frappe.get_all(
			"MS Timetable Slot", filters=filters, fields=["course"], limit_page_length=0
		)
		if r.course
	}
	if not names:
		return []

	return sorted(
		(
			{"id": r.name, "name": r.course_name or r.name}
			for r in frappe.get_all(
				"Course",
				filters={"name": ["in", list(names)]},
				fields=["name", "course_name"],
				limit_page_length=0,
			)
		),
		key=lambda r: r["name"],
	)


@frappe.whitelist()
@ms_endpoint(*STAFF)
def my_classes(persona: str = None):
	"""The classes the caller may open a connections panel for."""
	filters = {"disabled": 0}
	if persona == ROLE_TEACHER:
		scope = resolve_scope(persona)
		mine = set(scope.get("student_groups") or [])
		instructor = scope.get("instructor")
		if instructor:
			mine |= {
				r.student_group
				for r in frappe.get_all(
					"MS Timetable Slot",
					filters={"instructor": instructor, "active": 1},
					fields=["student_group"],
					limit_page_length=0,
				)
				if r.student_group
			}
		if not mine:
			return {"classes": []}
		filters["name"] = ["in", sorted(mine)]

	year = get_default_academic_year()
	if year and frappe.get_meta("Student Group").has_field("academic_year"):
		filters["academic_year"] = year

	rows = frappe.get_all(
		"Student Group",
		filters=filters,
		fields=["name", "student_group_name", "program", "batch"],
		order_by="program, batch, student_group_name",
		limit_page_length=0,
	)
	counts = {
		r.parent: r.n
		for r in frappe.db.sql(
			"""select parent, count(*) as n from `tabStudent Group Student`
			    where active = 1 group by parent""",
			as_dict=True,
		)
	}
	return {
		"classes": [
			{
				"id": r.name,
				"name": r.student_group_name,
				"program": r.program,
				"batch": r.batch,
				"students": counts.get(r.name, 0),
			}
			for r in rows
		]
	}
