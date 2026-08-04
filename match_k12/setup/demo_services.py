# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Demo data for the student-services modules.

Library, transport, health and behaviour shipped with only a couple of rows
each — enough to prove the endpoints worked, not enough to exercise paging,
filters or the exports. This seeds a realistic amount.
"""

import random

import frappe
from frappe.utils import add_days, today

BOOKS = [
	("الرياضيات للصف الأول", "د. سامي حمدان", "9789957001001", "رياضيات", 6),
	("العلوم العامة", "د. رانيا سمير", "9789957001002", "علوم", 5),
	("قواعد اللغة العربية", "أ. هالة القدسي", "9789957001003", "لغة عربية", 8),
	("English for Beginners", "J. Smith", "9789957001004", "لغة إنجليزية", 7),
	("أطلس العالم المصور", "دار المعرفة", "9789957001005", "مراجع", 3),
	("موسوعة الحيوانات", "دار الفراشة", "9789957001006", "علوم", 4),
	("حكايات من التراث", "أ. نور الهدى", "9789957001007", "أدب", 6),
	("مبادئ الفيزياء", "د. أحمد العبد الله", "9789957001008", "فيزياء", 5),
	("الكيمياء الحديثة", "د. سارة درويش", "9789957001009", "كيمياء", 4),
	("تاريخ فلسطين", "أ. كريم شاهين", "9789957001010", "اجتماعيات", 5),
	("الحاسوب للمبتدئين", "م. لينا النجار", "9789957001011", "حاسوب", 6),
	("رحلة في الفضاء", "دار العلوم", "9789957001012", "علوم", 3),
]

ROUTES = [
	("الخط الشمالي - بيرزيت", "أبو محمد", "0599111222", 30, 120),
	("الخط الجنوبي - بيتونيا", "أبو أحمد", "0599111333", 25, 110),
	("الخط الشرقي - الطيرة", "أبو خالد", "0599111444", 28, 130),
	("الخط الغربي - عين مصباح", "أبو يوسف", "0599111555", 22, 100),
]

CONDITIONS = ["", "", "", "ربو خفيف", "حساسية موسمية", "سكري من النوع الأول"]
ALLERGIES = ["", "", "", "المكسرات", "حبوب اللقاح", "اللاكتوز"]
BLOOD = ["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"]

VISIT_REASONS = [
	("صداع", "Illness"),
	("ألم في المعدة", "Illness"),
	("جرح بسيط في الركبة", "Injury"),
	("فحص دوري", "Checkup"),
	("ارتفاع في الحرارة", "Illness"),
	("رعاف", "Injury"),
]

POSITIVE = [
	("مشاركة متميزة في الحصة", 5, "Participation"),
	("مساعدة زميل", 3, "Helpfulness"),
	("تسليم الواجبات في وقتها", 4, "Homework"),
	("تفوق أكاديمي ملحوظ", 5, "Academic Excellence"),
]
NEGATIVE = [
	("تأخر عن الحصة", -2, "Late Arrival"),
	("عدم إحضار الكتاب", -1, "Homework"),
	("إزعاج أثناء الشرح", -3, "Disruption"),
]


def seed(students_limit: int = 60):
	"""Create demo rows for library, transport, health and behaviour."""
	random.seed(42)  # reproducible, so re-running does not churn the data

	students = frappe.get_all(
		"Student", filters={"enabled": 1}, fields=["name", "student_name"], limit=students_limit
	)
	if not students:
		return {"error": "no students"}

	created = {
		"books": _books(),
		"routes": _routes(),
	}
	created["loans"] = _loans(students)
	created["transport"] = _transport(students)
	created["health"] = _health(students)
	created["visits"] = _visits(students)
	created["behaviour"] = _behaviour(students)

	frappe.db.commit()
	return created


def _books() -> int:
	made = 0
	for title, author, isbn, category, copies in BOOKS:
		if frappe.db.exists("K12 Library Book", {"isbn": isbn}):
			continue
		frappe.get_doc(
			{
				"doctype": "K12 Library Book",
				"title": title,
				"author": author,
				"isbn": isbn,
				"category": category,
				"total_copies": copies,
				"available_copies": copies,
				"shelf": f"رف {random.randint(1, 12)}",
			}
		).insert(ignore_permissions=True)
		made += 1
	return made


def _routes() -> int:
	made = 0
	for name, driver, phone, capacity, fee in ROUTES:
		if frappe.db.exists("K12 Transport Route", {"route_name": name}):
			continue
		frappe.get_doc(
			{
				"doctype": "K12 Transport Route",
				"route_name": name,
				"driver_name": driver,
				"driver_phone": phone,
				"vehicle_number": f"{random.randint(10, 99)}-{random.randint(1000, 9999)}",
				"capacity": capacity,
				"monthly_fee": fee,
				"departure_time": "07:00:00",
				"return_time": "14:30:00",
				"active": 1,
			}
		).insert(ignore_permissions=True)
		made += 1
	return made


def _loans(students: list[dict]) -> int:
	books = frappe.get_all("K12 Library Book", fields=["name", "available_copies"], limit=20)
	if not books:
		return 0

	made = 0
	for student in students[:24]:
		book = random.choice(books)
		if frappe.db.exists(
			"K12 Book Loan", {"student": student.name, "book": book.name, "status": "Issued"}
		):
			continue
		issued = add_days(today(), -random.randint(1, 30))
		due = add_days(issued, 14)
		# A third of the loans are already back, so the filters have both states.
		returned = random.random() < 0.35
		frappe.get_doc(
			{
				"doctype": "K12 Book Loan",
				"book": book.name,
				"student": student.name,
				"issue_date": issued,
				"due_date": due,
				"return_date": add_days(due, -random.randint(0, 3)) if returned else None,
				"status": "Returned" if returned else "Issued",
			}
		).insert(ignore_permissions=True)
		made += 1
	return made


def _transport(students: list[dict]) -> int:
	# Only routes with room left, so seeding cannot trip the capacity check.
	routes = []
	for r in frappe.get_all("K12 Transport Route", fields=["name", "capacity"]):
		used = frappe.db.count("K12 Transport Assignment", {"route": r.name, "active": 1})
		free = (r.capacity or 0) - used
		if free > 0:
			routes.append({"name": r.name, "free": free})
	if not routes:
		return 0

	made = 0
	for student in students[:35]:
		if frappe.db.exists("K12 Transport Assignment", {"student": student.name}):
			continue
		routes = [r for r in routes if r["free"] > 0]
		if not routes:
			break
		route = random.choice(routes)
		route["free"] -= 1
		frappe.get_doc(
			{
				"doctype": "K12 Transport Assignment",
				"student": student.name,
				"route": route["name"],
				"stop": random.choice(
					["دوار الساعة", "مجمع البيرة", "شارع الإرسال", "دوار المنارة", "مفترق الطيرة"]
				),
				"start_date": add_days(today(), -random.randint(30, 120)),
				"active": 1,
			}
		).insert(ignore_permissions=True)
		made += 1
	return made


def _health(students: list[dict]) -> int:
	made = 0
	for student in students[:40]:
		if frappe.db.exists("K12 Health Record", {"student": student.name}):
			continue
		frappe.get_doc(
			{
				"doctype": "K12 Health Record",
				"student": student.name,
				"blood_group": random.choice(BLOOD),
				"height_cm": random.randint(120, 175),
				"weight_kg": random.randint(25, 70),
				"chronic_conditions": random.choice(CONDITIONS),
				"allergies": random.choice(ALLERGIES),
				"emergency_contact_name": "ولي الأمر",
				"emergency_contact_phone": f"05{random.randint(90000000, 99999999)}",
				"last_checkup": add_days(today(), -random.randint(30, 300)),
			}
		).insert(ignore_permissions=True)
		made += 1
	return made


def _visits(students: list[dict]) -> int:
	made = 0
	for student in students[:30]:
		for _ in range(random.randint(0, 2)):
			reason, visit_type = random.choice(VISIT_REASONS)
			frappe.get_doc(
				{
					"doctype": "K12 Health Visit",
					"student": student.name,
					"visit_date": add_days(today(), -random.randint(1, 90)),
					"visit_type": visit_type,
					"complaint": reason,
					"treatment": "راحة ومتابعة",
					"outcome": random.choice(
						["Returned to Class", "Sent Home", "Rest in Clinic"]
					),
				}
			).insert(ignore_permissions=True)
			made += 1
	return made


def _behaviour(students: list[dict]) -> int:
	instructors = frappe.get_all("Instructor", pluck="name", limit=10)
	made = 0
	for student in students[:45]:
		for _ in range(random.randint(0, 3)):
			positive = random.random() < 0.65
			note, points, category = random.choice(POSITIVE if positive else NEGATIVE)
			frappe.get_doc(
				{
					"doctype": "K12 Behaviour Record",
					"student": student.name,
					"record_date": add_days(today(), -random.randint(1, 60)),
					"record_type": "Positive" if positive else "Negative",
					"category": category,
					"description": note,
					"points": points,
					"reported_by": random.choice(instructors) if instructors else None,
				}
			).insert(ignore_permissions=True)
			made += 1
	return made
