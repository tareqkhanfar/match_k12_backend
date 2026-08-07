# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Seed realistic Arabic demo data for the Match Schools frontend.

Run with:
    bench --site <site> execute match_schools.setup.demo_data.create_demo_data
"""

import random

import frappe
from frappe.utils import add_days, add_months, getdate, today

FIRST_NAMES_M = ["أحمد", "محمد", "يوسف", "عمر", "خالد", "زيد", "كريم", "مالك", "بشار", "سامي", "أنس", "حمزة"]
FIRST_NAMES_F = ["مريم", "فاطمة", "ليان", "سلمى", "نور", "رزان", "هبة", "دانا", "جنى", "لينا", "آية", "سارة"]
FAMILIES = ["العبد الله", "أبو راس", "الخطيب", "حمدان", "المصري", "درويش", "الحلبي", "السعدي", "قاسم", "النجار", "شاهين", "زيدان"]
CITIES = ["رام الله", "نابلس", "الخليل", "غزة", "بيت لحم", "جنين"]

GRADES = [
	"الصف الأول", "الصف الثاني", "الصف الثالث", "الصف الرابع", "الصف الخامس", "الصف السادس",
	"الصف السابع", "الصف الثامن", "الصف التاسع", "الصف العاشر", "الصف الحادي عشر", "الصف الثاني عشر",
]
SECTIONS = ["أ", "ب", "ج"]

SUBJECTS = [
	("الرياضيات", "MTH"), ("اللغة العربية", "ARB"), ("اللغة الإنجليزية", "ENG"),
	("العلوم", "SCI"), ("الفيزياء", "PHY"), ("الكيمياء", "CHM"),
	("الأحياء", "BIO"), ("التربية الإسلامية", "ISL"), ("الاجتماعيات", "SOC"),
	("الحاسوب", "CMP"), ("الرياضة", "SPT"), ("الفنون", "ART"),
]

QUALIFICATIONS = ["بكالوريوس تربية", "ماجستير مناهج", "بكالوريوس علوم", "ماجستير إدارة تربوية"]


def create_demo_data(students_per_group: int = 12):
	"""Create a full demo school. Idempotent-ish: skips records that exist."""
	random.seed(42)
	frappe.flags.in_import = True

	year = _ensure_academic_year()
	term = _ensure_academic_term(year)
	_ensure_batches()
	_ensure_holiday_list(year)
	rooms = _ensure_rooms()
	courses = _ensure_courses()
	programs = _ensure_programs(courses)
	instructors = _ensure_instructors(courses)
	groups = _ensure_student_groups(programs, year, term, instructors)
	students = _ensure_students(groups, programs, year, students_per_group)
	_ensure_schedules(groups, instructors, courses, rooms)
	_ensure_attendance(groups)
	_ensure_assignments(groups, instructors, courses, students)
	_ensure_announcements()

	frappe.db.commit()
	return {
		"academic_year": year,
		"academic_term": term,
		"programs": len(programs),
		"courses": len(courses),
		"instructors": len(instructors),
		"student_groups": len(groups),
		"students": len(students),
	}


def _ensure_academic_year() -> str:
	"""Academic year spanning today, so schedules/terms validate cleanly."""
	start = getdate(add_months(today(), -11))
	end = getdate(add_months(today(), 1))
	name = f"{start.year}-{end.year}"
	if not frappe.db.exists("Academic Year", name):
		frappe.get_doc(
			{
				"doctype": "Academic Year",
				"academic_year_name": name,
				"year_start_date": start,
				"year_end_date": end,
			}
		).insert(ignore_permissions=True)
	frappe.db.set_single_value("Education Settings", "current_academic_year", name)
	return name


def _ensure_academic_term(year: str) -> str:
	term_name = f"{year} (الفصل الثاني)"
	if not frappe.db.exists("Academic Term", term_name):
		frappe.get_doc(
			{
				"doctype": "Academic Term",
				"academic_year": year,
				"term_name": "الفصل الثاني",
				"term_start_date": getdate(add_months(today(), -5)),
				"term_end_date": getdate(add_months(today(), 1)),
			}
		).insert(ignore_permissions=True)
	frappe.db.set_single_value("Education Settings", "current_academic_term", term_name)
	return term_name


def _ensure_batches():
	"""Sections (أ / ب / ج) are modelled as Student Batch Name records."""
	for section in SECTIONS:
		if not frappe.db.exists("Student Batch Name", section):
			frappe.get_doc(
				{"doctype": "Student Batch Name", "batch_name": section}
			).insert(ignore_permissions=True)


def _ensure_holiday_list(year: str):
	"""Course Schedule needs the company to have a default holiday list."""
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value("Company", {}, "name")
	if not company:
		return
	if frappe.db.get_value("Company", company, "default_holiday_list"):
		return

	list_name = f"عطل {year}"
	if not frappe.db.exists("Holiday List", list_name):
		start = frappe.db.get_value("Academic Year", year, "year_start_date")
		end = frappe.db.get_value("Academic Year", year, "year_end_date")
		doc = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": list_name,
				"from_date": start,
				"to_date": end,
				"weekly_off": "Friday",
			}
		)
		doc.insert(ignore_permissions=True)
		doc.get_weekly_off_dates()
		doc.save(ignore_permissions=True)

	frappe.db.set_value("Company", company, "default_holiday_list", list_name)


def _ensure_rooms(count: int = 8) -> list[str]:
	"""Course Schedule requires a room, so create a small pool of classrooms."""
	names = []
	for i in range(count):
		room_name = f"قاعة {101 + i}"
		existing = frappe.db.get_value("Room", {"room_name": room_name}, "name")
		if existing:
			names.append(existing)
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Room",
				"room_name": room_name,
				"room_number": str(101 + i),
				"seating_capacity": "35",
			}
		).insert(ignore_permissions=True)
		names.append(doc.name)
	return names


def _ensure_courses() -> list[str]:
	names = []
	for subject, code in SUBJECTS:
		if not frappe.db.exists("Course", subject):
			frappe.get_doc(
				{
					"doctype": "Course",
					"course_name": subject,
					"course_code": code,
					"is_published": 1,
				}
			).insert(ignore_permissions=True)
		names.append(subject)
	return names


def _ensure_programs(courses: list[str]) -> list[str]:
	created = []
	for grade in GRADES:
		if not frappe.db.exists("Program", grade):
			doc = frappe.get_doc(
				{
					"doctype": "Program",
					"program_name": grade,
					"program_abbreviation": grade.replace("الصف ", "")[:10],
				}
			)
			for course in courses[:7]:
				doc.append("courses", {"course": course, "required": 1})
			doc.insert(ignore_permissions=True)
		created.append(grade)
	return created


def _ensure_instructors(courses: list[str]) -> list[str]:
	created = []
	for i in range(len(SUBJECTS)):
		male = i % 2 == 0
		first = (FIRST_NAMES_M if male else FIRST_NAMES_F)[i % 12]
		family = FAMILIES[(i * 7) % len(FAMILIES)]
		name = f"أ. {first} {family}"
		existing = frappe.db.get_value("Instructor", {"instructor_name": name}, "name")
		if existing:
			created.append(existing)
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Instructor",
				"instructor_name": name,
				"gender": "Male" if male else "Female",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		created.append(doc.name)
	return created


def _ensure_student_groups(programs, year, term, instructors) -> list[dict]:
	groups = []
	for gi, program in enumerate(programs):
		# Two sections for most grades, three for every third grade.
		for si in range(3 if gi % 3 == 0 else 2):
			section = SECTIONS[si]
			group_name = f"{program} - {section}"
			existing = frappe.db.get_value("Student Group", {"student_group_name": group_name}, "name")
			if existing:
				groups.append({"name": existing, "program": program, "section": section})
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Student Group",
					"student_group_name": group_name,
					"group_based_on": "Batch",
					"program": program,
					"batch": section,
					"academic_year": year,
					"academic_term": term,
					"max_strength": 35,
				}
			)
			doc.append("instructors", {"instructor": instructors[(gi + si) % len(instructors)]})
			doc.insert(ignore_permissions=True)
			groups.append({"name": doc.name, "program": program, "section": section})
	return groups


def _ensure_students(groups, programs, year, per_group) -> list[str]:
	created = []
	counter = 0
	for group in groups:
		group_doc = frappe.get_doc("Student Group", group["name"])
		existing_students = {r.student for r in group_doc.students}

		for _ in range(per_group):
			counter += 1
			male = counter % 2 == 0
			first = (FIRST_NAMES_M if male else FIRST_NAMES_F)[counter % 12]
			family = FAMILIES[(counter * 5) % len(FAMILIES)]
			# Education creates a Customer named after the student, and Customer
			# names must be unique — the middle name plus a serial keeps every
			# generated student distinct even when first/last names repeat.
			middle = FIRST_NAMES_M[(counter * 3) % 12]
			family = f"{family} {counter}"
			full_name = f"{first} {middle} {family}"

			student_id = frappe.db.get_value(
				"Student", {"student_name": full_name, "date_of_birth": _birth_date(counter)}, "name"
			)
			if not student_id:
				student = frappe.get_doc(
					{
						"doctype": "Student",
						"first_name": first,
						"middle_name": middle,
						"last_name": family,
						"gender": "Male" if male else "Female",
						"date_of_birth": _birth_date(counter),
						"student_email_id": f"student{counter}@match-edu.ps",
						"address_line_1": CITIES[counter % len(CITIES)],
						"joining_date": "2025-09-01",
						"enabled": 1,
					}
				).insert(ignore_permissions=True)
				student_id = student.name

			created.append(student_id)

			# Program Enrollment ties the student to a grade for the year.
			if not frappe.db.exists(
				"Program Enrollment",
				{"student": student_id, "program": group["program"], "academic_year": year, "docstatus": ["<", 2]},
			):
				enrollment = frappe.get_doc(
					{
						"doctype": "Program Enrollment",
						"student": student_id,
						"student_name": full_name,
						"program": group["program"],
						"academic_year": year,
						"enrollment_date": "2025-09-01",
						"student_batch_name": group["section"],
					}
				)
				enrollment.insert(ignore_permissions=True)
				enrollment.submit()

			if student_id not in existing_students:
				group_doc.append("students", {"student": student_id, "student_name": full_name, "active": 1})
				existing_students.add(student_id)

		group_doc.save(ignore_permissions=True)
	return created


def _birth_date(i: int) -> str:
	year = 2008 + (i % 11)
	month = (i % 12) + 1
	day = (i % 27) + 1
	return f"{year}-{month:02d}-{day:02d}"


def _ensure_schedules(groups, instructors, courses, rooms):
	"""Create a week of course schedules for the first few groups."""
	start = getdate(today())
	for gi, group in enumerate(groups[:6]):
		for day_offset in range(5):
			date = add_days(start, day_offset)
			# Skip the weekend (Friday/Saturday in Palestine).
			if getdate(date).weekday() in (4, 5):
				continue
			for period in range(4):
				course = courses[(gi + period) % len(courses)]
				instructor = instructors[(gi + period) % len(instructors)]
				exists = frappe.db.exists(
					"Course Schedule",
					{
						"student_group": group["name"],
						"schedule_date": date,
						"from_time": f"{8 + period}:00:00",
					},
				)
				if exists:
					continue
				frappe.get_doc(
					{
						"doctype": "Course Schedule",
						"student_group": group["name"],
						"course": course,
						"instructor": instructor,
						"schedule_date": date,
						"from_time": f"{8 + period}:00:00",
						"to_time": f"{8 + period}:45:00",
						"room": rooms[(gi + period) % len(rooms)] if rooms else None,
					}
				).insert(ignore_permissions=True)


def _ensure_attendance(groups):
	"""Attendance for the last 60 days so trend charts have data."""
	start = add_months(today(), -2)
	for group in groups[:6]:
		group_doc = frappe.get_doc("Student Group", group["name"])
		student_ids = [r.student for r in group_doc.students][:10]
		if not student_ids:
			continue
		date = getdate(start)
		end = getdate(today())
		while date <= end:
			# Skip Friday/Saturday (weekend in Palestine).
			if date.weekday() in (4, 5):
				date = add_days(date, 1)
				continue
			for student in student_ids:
				if frappe.db.exists(
					"Student Attendance",
					{"student": student, "date": date, "student_group": group["name"], "docstatus": ["<", 2]},
				):
					continue
				status = "Present" if random.random() > 0.12 else "Absent"
				doc = frappe.get_doc(
					{
						"doctype": "Student Attendance",
						"student": student,
						"student_group": group["name"],
						"date": date,
						"status": status,
					}
				)
				doc.insert(ignore_permissions=True)
				doc.submit()
			date = add_days(date, 7)  # weekly sample keeps the dataset small


def _ensure_assignments(groups, instructors, courses, students):
	titles = [
		"حل تمارين الوحدة الرابعة",
		"تحليل قصيدة المتنبي",
		"تقرير تجربة الكثافة",
		"Essay: My Future Career",
		"بحث عن دورة الماء",
		"خريطة ذهنية للدولة الأموية",
	]
	for i, title in enumerate(titles):
		group = groups[i % len(groups)]
		if frappe.db.exists("MS Assignment", {"title": title, "student_group": group["name"]}):
			continue
		frappe.get_doc(
			{
				"doctype": "MS Assignment",
				"title": title,
				"course": courses[i % len(courses)],
				"student_group": group["name"],
				"instructor": instructors[i % len(instructors)],
				"assigned_on": add_days(today(), -7),
				"due_date": add_days(today(), 3 + i),
				"maximum_score": 100,
				"status": "Open",
				"description": f"<p>{title}</p>",
			}
		).insert(ignore_permissions=True)


def _ensure_announcements():
	items = [
		("الاجتماع الفصلي لأولياء الأمور", "يُعقد اجتماع أولياء الأمور لمناقشة نتائج الفصل الأول في قاعة المدرسة الكبرى.", "Event", "أولياء الأمور"),
		("بدء امتحانات الفصل الثاني", "تبدأ امتحانات الفصل الثاني للصفوف من السابع حتى الثاني عشر وفق الجدول المعلن.", "Announcement", "الطلاب والمعلمون"),
		("رحلة علمية إلى متحف العلوم", "رحلة لطلاب الصفوف الخامس والسادس، الرجاء تسليم موافقة ولي الأمر.", "Event", "الصف الخامس والسادس"),
		("تذكير بسداد الرسوم المتأخرة", "نرجو من أولياء الأمور المتأخرين سداد الأقساط قبل نهاية الشهر.", "Alert", "أولياء الأمور"),
		("يوم رياضي مفتوح", "منافسات رياضية بين الشُعب في ملعب المدرسة مع جوائز للفرق الفائزة.", "Event", "جميع الطلاب"),
	]
	for i, (title, body, atype, audience) in enumerate(items):
		if frappe.db.exists("MS Announcement", {"title": title}):
			continue
		frappe.get_doc(
			{
				"doctype": "MS Announcement",
				"title": title,
				"body": f"<p>{body}</p>",
				"announcement_type": atype,
				"audience": "All",
				"audience_label": audience,
				"posted_on": add_days(today(), i - 2),
				"published": 1,
			}
		).insert(ignore_permissions=True)
