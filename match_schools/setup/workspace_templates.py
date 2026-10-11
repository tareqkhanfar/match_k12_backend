"""Ready-made workspaces — one per kind of staff member.

A template is only a starting point: creating one makes an ordinary
`MS Workspace` the Administrator edits like any other, and the template stays
here to start from again. The three new staff roles get theirs created once,
as their default, so a vice principal, supervisor or nurse signs in to a
screen made for them; the others are offered in the builder's gallery.

Buttons name screens by route; a screen a role cannot open drops out of that
role's view on its own (see the web app's `resolveItem`).
"""

import json

import frappe

SEEDED_KEY = "ms_workspace_templates_seeded_v1"


def _screen(route: str, label: str = "", icon: str = "", color: str = "") -> dict:
	return {"kind": "screen", "target": route, "label": label, "icon": icon, "color": color}


TEMPLATES = {
	"vice_principal": {
		"title": "مساحة نائب المدير",
		"icon": "target",
		"color": "violet",
		"description": "متابعة المعلمين والطلاب من الأعلى: التقارير، العلامات، الحضور، المناوبات.",
		"roles": ["vice_principal"],
		"blocks": [
			{"type": "numbers", "title": "اليوم", "cards": ["attendance_today", "students", "instructors", "admissions", "unread_mail"]},
			{
				"type": "shortcuts",
				"title": "القيادة والمتابعة",
				"items": [
					_screen("/app/leadership-reports", "تقارير القيادة", "bar-chart", "violet"),
					_screen("/app/day-schedule", "جدول اليوم والمناوبات", "calendar-clock", "blue"),
					_screen("/app/attendance", "", "user-check", "green"),
					_screen("/app/gradebook", "رصد العلامات", "clipboard-check", "orange"),
					_screen("/app/term", "", "award", "amber"),
					_screen("/app/class-log", "", "notebook-pen", "teal"),
				],
			},
			{
				"type": "links",
				"title": "المعلمون",
				"items": [
					_screen("/app/teachers"),
					_screen("/app/timetable-teacher"),
					_screen("/app/appraisal"),
					_screen("/app/periodic-evaluations"),
					_screen("/app/exams"),
				],
			},
			{
				"type": "links",
				"title": "الطلاب",
				"items": [
					_screen("/app/students"),
					_screen("/app/record"),
					_screen("/app/quarter-results"),
					_screen("/app/behaviour"),
					_screen("/app/alerts"),
				],
			},
			{
				"type": "links",
				"title": "التواصل",
				"items": [_screen("/app/mail"), _screen("/app/community"), _screen("/app/calendar")],
			},
		],
	},
	"supervisor": {
		"title": "مساحة المشرف",
		"icon": "graduation-cap",
		"color": "teal",
		"description": "اطّلاع على الشعب والصفوف التي تشرف عليها — الأمور الأكاديمية فقط.",
		"roles": ["supervisor"],
		"blocks": [
			{
				"type": "text",
				"title": "حساب للاطلاع",
				"text": "تعرض هذه الشاشات الشعب والصفوف التي تشرف عليها فقط، ولا يمكن التعديل منها.",
			},
			{
				"type": "shortcuts",
				"title": "شعبي",
				"items": [
					_screen("/app/students", "طلاب شعبي", "users", "teal"),
					_screen("/app/attendance", "", "user-check", "green"),
					_screen("/app/record", "علامات الطلبة", "clipboard-check", "orange"),
					_screen("/app/timetable", "", "calendar-days", "blue"),
					_screen("/app/exams", "", "file-check", "violet"),
					_screen("/app/class-log", "", "notebook-pen", "amber"),
				],
			},
			{
				"type": "links",
				"title": "التقييم والنتائج",
				"items": [
					_screen("/app/gradebook"),
					_screen("/app/quarter-results"),
					_screen("/app/periodic-evaluations"),
					_screen("/app/finals"),
				],
			},
			{
				"type": "links",
				"title": "المحتوى",
				"items": [_screen("/app/assignments"), _screen("/app/quizzes"), _screen("/app/resources")],
			},
			{"type": "links", "title": "عام", "items": [_screen("/app/calendar"), _screen("/app/mail")]},
		],
	},
	"nurse": {
		"title": "مساحة التمريض",
		"icon": "stethoscope",
		"color": "red",
		"description": "الملفات الصحية وزيارات العيادة ونماذج التمريض وتقاريرها.",
		"roles": ["nurse"],
		"blocks": [
			{
				"type": "shortcuts",
				"title": "التمريض",
				"items": [
					_screen("/app/health", "الملف الصحي والزيارات", "heart-pulse", "red"),
					_screen("/app/forms/nursing", "نماذج التمريض", "clipboard-list", "pink"),
					_screen("/app/health-reports", "تقارير التمريض", "bar-chart", "violet"),
				],
			},
			{
				"type": "links",
				"title": "عام",
				"items": [_screen("/app/mail"), _screen("/app/community"), _screen("/app/help")],
			},
		],
	},
	"secretary": {
		"title": "مساحة السكرتارية",
		"icon": "folder-open",
		"color": "blue",
		"description": "القبول والتسجيل والطلاب والرسوم وطابور الطباعة.",
		"roles": [],
		"blocks": [
			{"type": "numbers", "title": "", "cards": ["students", "admissions", "unpaid", "print"]},
			{
				"type": "shortcuts",
				"title": "العمل اليومي",
				"items": [
					_screen("/app/admissions", "", "user-plus", "blue"),
					_screen("/app/enrollment", "", "book-open-check", "green"),
					_screen("/app/students", "", "users", "teal"),
					_screen("/app/fees", "", "wallet", "amber"),
					_screen("/app/print-requests", "", "printer", "gray"),
				],
			},
			{
				"type": "links",
				"title": "الطلبات",
				"items": [_screen("/app/admission-requests"), _screen("/app/transfers"), _screen("/app/certificates")],
			},
		],
	},
	"teacher": {
		"title": "مساحة المعلم",
		"icon": "book-open",
		"color": "green",
		"description": "الحضور والعلامات ودفتر الحصص والواجبات.",
		"roles": [],
		"blocks": [
			{"type": "numbers", "title": "", "cards": ["my_classes", "my_students", "to_grade", "class_log"]},
			{
				"type": "shortcuts",
				"title": "حصصي",
				"items": [
					_screen("/app/attendance", "", "user-check", "green"),
					_screen("/app/gradebook", "", "clipboard-check", "orange"),
					_screen("/app/class-log", "", "notebook-pen", "teal"),
					_screen("/app/assignments", "", "file-text", "blue"),
					_screen("/app/timetable", "", "calendar-days", "violet"),
				],
			},
			{
				"type": "links",
				"title": "المزيد",
				"items": [_screen("/app/exams"), _screen("/app/resources"), _screen("/app/periodic-evaluations")],
			},
		],
	},
}


def create_from_template(key: str, default_roles: list[str] | None = None) -> str:
	"""A new, ordinary workspace from the template; returns its name."""
	from match_schools.api.workspaces import normalize_layout

	t = TEMPLATES[key]
	doc = frappe.new_doc("MS Workspace")
	doc.title = t["title"]
	doc.icon = t["icon"]
	doc.color = t["color"]
	doc.description = t["description"]
	doc.is_active = 1
	doc.personas = json.dumps(t["roles"])
	doc.default_roles = json.dumps(t["roles"] if default_roles is None else default_roles)
	doc.layout = json.dumps(normalize_layout({"blocks": t["blocks"]}), ensure_ascii=False)
	doc.insert(ignore_permissions=True)
	return doc.name


def seed():
	"""Once per site: the three new staff roles get their workspace, as their
	default. Run from migrate; editing or deleting them afterwards is kept."""
	if frappe.db.get_default(SEEDED_KEY):
		return
	if not frappe.db.table_exists("MS Workspace") or not frappe.get_meta("MS Workspace").has_field("default_roles"):
		return
	for key in ("vice_principal", "supervisor", "nurse"):
		if not frappe.db.exists("MS Workspace", {"title": TEMPLATES[key]["title"]}):
			create_from_template(key)
	frappe.db.set_default(SEEDED_KEY, "1")
