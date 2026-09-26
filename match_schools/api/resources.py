# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Library (books and loans) and school transport."""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from match_schools.api.utils import (
	period_conditions,
	anchor_term,
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	build_conditions,
	build_order_by,
	fail,
	ms_endpoint,
	paginate,
	parse_json_arg,
	resolve_scope,
)

LOAN_STATUS_AR = {
	"Issued": "مُعارة",
	"Returned": "أُعيدت",
	"Overdue": "متأخرة",
	"Lost": "مفقودة",
}

BOOK_FILTERS = {
	"search": "title",
	"category": "category",
	"language": "language",
	"author": "author",
}
LOAN_FILTERS = {
	"student": "student",
	"book": "book",
	"status": "status",
	"search": "student_name",
}


def _own_students(persona: str) -> list[str] | None:
	if persona in BACK_OFFICE or persona == ROLE_TEACHER:
		return None
	return resolve_scope(persona).get("students") or []


# --- Library: books --------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_books(
	filters: str | dict = None,
	page: int = 1,
	page_size: int = 25,
	sort_field: str = None,
	sort_order: str = None,
	persona: str = None,
):
	filters = parse_json_arg(filters) or {}
	page, page_size, offset = paginate(page, page_size)

	conditions = ["1=1"]
	params: dict = {}

	# Free-text search spans title and author.
	if filters.get("search"):
		conditions.append("(title LIKE %(search)s OR author LIKE %(search)s OR isbn LIKE %(search)s)")
		params["search"] = f"%{filters.pop('search')}%"
	# "Available only" is derived, not a stored column.
	available_only = filters.pop("available_only", None)
	if cint(available_only):
		conditions.append("available_copies > 0")

	conditions += build_conditions(filters, BOOK_FILTERS, params)
	where = " AND ".join(conditions)
	order_by = build_order_by(sort_field, sort_order, BOOK_FILTERS, "title ASC")

	total = frappe.db.sql(
		f"SELECT COUNT(*) AS total FROM `tabMS Library Book` WHERE {where}", params, as_dict=True
	)[0].total

	params["limit"], params["offset"] = page_size, offset
	rows = frappe.db.sql(
		f"""
		SELECT name, title, author, isbn, category, language, publisher,
			published_year, shelf, total_copies, available_copies, cover_image
		FROM `tabMS Library Book`
		WHERE {where}
		ORDER BY {order_by}
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		params,
		as_dict=True,
	)

	return {
		"items": [
			{
				"id": r.name,
				"title": r.title,
				"author": r.author,
				"isbn": r.isbn,
				"category": r.category,
				"language": r.language,
				"publisher": r.publisher,
				"published_year": r.published_year,
				"shelf": r.shelf,
				"total_copies": cint(r.total_copies),
				"available_copies": cint(r.available_copies),
				"cover_image": r.cover_image,
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_book(payload: str | dict, persona: str = None):
	data = parse_json_arg(payload) or {}
	if not data.get("title"):
		return fail(message_en="Title is required.", message_ar="عنوان الكتاب مطلوب.")

	fields = {
		k: data.get(k)
		for k in (
			"title", "author", "isbn", "category", "language", "publisher",
			"published_year", "shelf", "total_copies", "description", "cover_image",
		)
		if data.get(k) is not None
	}

	book_id = data.get("id") or data.get("name")
	if book_id:
		doc = frappe.get_doc("MS Library Book", book_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Book updated.", "تم تحديث الكتاب."
	else:
		doc = frappe.get_doc({"doctype": "MS Library Book", **fields})
		doc.insert()
		msg_en, msg_ar = "Book added.", "تمت إضافة الكتاب."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_book(book: str, persona: str = None):
	on_loan = frappe.db.count("MS Book Loan", {"book": book, "status": ["in", ["Issued", "Overdue"]]})
	if on_loan:
		return fail(
			message_en="This book still has copies on loan.",
			message_ar="لا يمكن الحذف: توجد نسخ معارة من هذا الكتاب.",
		)
	frappe.delete_doc("MS Library Book", book, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": book},
		"message_en": "Book deleted.",
		"message_ar": "تم حذف الكتاب.",
	}


# --- Library: loans --------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_loans(
	filters: str | dict = None,
	page: int = 1,
	page_size: int = 25,
	sort_field: str = None,
	sort_order: str = None,
	persona: str = None,
):
	filters = parse_json_arg(filters) or {}
	page, page_size, offset = paginate(page, page_size)

	conditions = ["1=1"]
	params: dict = {}

	allowed = _own_students(persona)
	if allowed is not None:
		if not allowed:
			return {"items": [], "total": 0, "page": page, "page_size": page_size, "summary": {}}
		conditions.append("student IN %(allowed)s")
		params["allowed"] = allowed

	if filters.get("search"):
		conditions.append("(student_name LIKE %(search)s OR book_title LIKE %(search)s)")
		params["search"] = f"%{filters.pop('search')}%"

	conditions += build_conditions(filters, LOAN_FILTERS, params)
	period_conditions("MS Book Loan", conditions, params)
	where = " AND ".join(conditions)
	order_by = build_order_by(sort_field, sort_order, LOAN_FILTERS, "issue_date DESC")

	total = frappe.db.sql(
		f"SELECT COUNT(*) AS total FROM `tabMS Book Loan` WHERE {where}", params, as_dict=True
	)[0].total

	params["limit"], params["offset"] = page_size, offset
	rows = frappe.db.sql(
		f"""
		SELECT name, book, book_title, student, student_name, status,
			issue_date, due_date, return_date, notes
		FROM `tabMS Book Loan`
		WHERE {where}
		ORDER BY {order_by}
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		params,
		as_dict=True,
	)

	counts = frappe.db.sql(
		f"""
		SELECT status, COUNT(*) AS count
		FROM `tabMS Book Loan` WHERE {where}
		GROUP BY status
		""",
		{k: v for k, v in params.items() if k not in ("limit", "offset")},
		as_dict=True,
	)

	return {
		"items": [
			{
				"id": r.name,
				"book": r.book,
				"book_title": r.book_title,
				"student": r.student,
				"student_name": r.student_name,
				"status": LOAN_STATUS_AR.get(r.status, r.status),
				"status_raw": r.status,
				"issue_date": str(r.issue_date or ""),
				"due_date": str(r.due_date or ""),
				"return_date": str(r.return_date or ""),
				"notes": r.notes,
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
		"summary": {r.status: r.count for r in counts},
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def issue_book(payload: str | dict, persona: str = None):
	"""Issue a book to a student."""
	data = parse_json_arg(payload) or {}
	if not data.get("book") or not data.get("student"):
		return fail(
			message_en="Book and student are required.",
			message_ar="الكتاب والطالب مطلوبان.",
		)

	issue_date = data.get("issue_date") or today()
	doc = frappe.get_doc(
		{
			"doctype": "MS Book Loan",
			"book": data["book"],
			"student": data["student"],
			"issue_date": issue_date,
			"due_date": data.get("due_date") or add_days(issue_date, 14),
			"status": "Issued",
			"notes": data.get("notes"),
		}
	)
	anchor_term(doc, doc.get("issue_date"))
	doc.insert()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Book issued.",
		"message_ar": "تم إعارة الكتاب.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def return_book(loan: str, status: str = "Returned", persona: str = None):
	"""Mark a loan returned (or lost)."""
	if status not in ("Returned", "Lost"):
		return fail(message_en="Invalid status.", message_ar="حالة غير صالحة.")

	doc = frappe.get_doc("MS Book Loan", loan)
	doc.status = status
	if status == "Returned":
		doc.return_date = today()
	doc.save()
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "status": doc.status},
		"message_en": "Loan updated.",
		"message_ar": "تم تحديث الإعارة.",
	}


# --- Transport -------------------------------------------------------------

ROUTE_FILTERS = {"search": "route_name", "active": "active", "driver_name": "driver_name"}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_routes(filters: str | dict = None, persona: str = None):
	filters = parse_json_arg(filters) or {}
	conditions = ["1=1"]
	params: dict = {}

	if filters.get("search"):
		conditions.append("(route_name LIKE %(search)s OR vehicle_number LIKE %(search)s)")
		params["search"] = f"%{filters.pop('search')}%"

	conditions += build_conditions(filters, ROUTE_FILTERS, params)
	where = " AND ".join(conditions)

	rows = frappe.db.sql(
		f"""
		SELECT name, route_name, vehicle_number, driver_name, driver_phone,
			capacity, departure_time, return_time, active, monthly_fee, stops
		FROM `tabMS Transport Route`
		WHERE {where}
		ORDER BY route_name
		""",
		params,
		as_dict=True,
	)

	out = []
	for r in rows:
		assigned = frappe.db.count("MS Transport Assignment", {"route": r.name, "active": 1})
		out.append(
			{
				"id": r.name,
				"route_name": r.route_name,
				"vehicle_number": r.vehicle_number,
				"driver_name": r.driver_name,
				"driver_phone": r.driver_phone,
				"capacity": cint(r.capacity),
				"assigned": assigned,
				"seats_left": max(cint(r.capacity) - assigned, 0),
				"departure_time": str(r.departure_time or ""),
				"return_time": str(r.return_time or ""),
				"active": bool(r.active),
				"monthly_fee": flt(r.monthly_fee),
				"stops": [s.strip() for s in (r.stops or "").splitlines() if s.strip()],
			}
		)
	return out


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_route(payload: str | dict, persona: str = None):
	data = parse_json_arg(payload) or {}
	if not data.get("route_name"):
		return fail(message_en="Route name is required.", message_ar="اسم الخط مطلوب.")

	fields = {
		k: data.get(k)
		for k in (
			"route_name", "vehicle_number", "driver_name", "driver_phone", "capacity",
			"departure_time", "return_time", "active", "monthly_fee", "stops",
		)
		if data.get(k) is not None
	}
	# The UI sends stops as a list.
	if isinstance(fields.get("stops"), list):
		fields["stops"] = "\n".join(fields["stops"])

	route_id = data.get("id") or data.get("name")
	if route_id:
		doc = frappe.get_doc("MS Transport Route", route_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Route updated.", "تم تحديث الخط."
	else:
		doc = frappe.get_doc({"doctype": "MS Transport Route", **fields})
		doc.insert()
		msg_en, msg_ar = "Route added.", "تمت إضافة الخط."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_route(route: str, persona: str = None):
	assigned = frappe.db.count("MS Transport Assignment", {"route": route, "active": 1})
	if assigned:
		return fail(
			message_en="This route still has active student assignments.",
			message_ar="لا يمكن الحذف: يوجد طلاب مسندون لهذا الخط.",
		)
	frappe.delete_doc("MS Transport Route", route, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": route},
		"message_en": "Route deleted.",
		"message_ar": "تم حذف الخط.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_transport_assignments(
	filters: str | dict = None, page: int = 1, page_size: int = 50, persona: str = None
):
	filters = parse_json_arg(filters) or {}
	page, page_size, offset = paginate(page, page_size)

	conditions = ["1=1"]
	params: dict = {}

	allowed = _own_students(persona)
	if allowed is not None:
		if not allowed:
			return {"items": [], "total": 0, "page": page, "page_size": page_size}
		conditions.append("student IN %(allowed)s")
		params["allowed"] = allowed

	for key in ("route", "student"):
		if filters.get(key):
			conditions.append(f"{key} = %({key})s")
			params[key] = filters[key]
	if filters.get("search"):
		conditions.append("student_name LIKE %(search)s")
		params["search"] = f"%{filters['search']}%"
	if filters.get("active") not in (None, ""):
		conditions.append("active = %(active)s")
		params["active"] = cint(filters["active"])

	# A bus seat is allocated for a year: the term is not part of the filter,
	# or a pupil would vanish from the route list halfway through the year.
	period_conditions("MS Transport Assignment", conditions, params, by_term=False)

	where = " AND ".join(conditions)
	total = frappe.db.sql(
		f"SELECT COUNT(*) AS total FROM `tabMS Transport Assignment` WHERE {where}",
		params,
		as_dict=True,
	)[0].total

	params["limit"], params["offset"] = page_size, offset
	rows = frappe.db.sql(
		f"""
		SELECT name, student, student_name, route, stop, active,
			start_date, end_date, notes
		FROM `tabMS Transport Assignment`
		WHERE {where}
		ORDER BY student_name
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		params,
		as_dict=True,
	)

	route_names = {r.route for r in rows if r.route}
	labels = {
		n: frappe.db.get_value("MS Transport Route", n, "route_name") for n in route_names
	}

	return {
		"items": [
			{
				"id": r.name,
				"student": r.student,
				"student_name": r.student_name,
				"route": r.route,
				"route_name": labels.get(r.route),
				"stop": r.stop,
				"active": bool(r.active),
				"start_date": str(r.start_date or ""),
				"end_date": str(r.end_date or ""),
				"notes": r.notes,
			}
			for r in rows
		],
		"total": total,
		"page": page,
		"page_size": page_size,
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def save_transport_assignment(payload: str | dict, persona: str = None):
	data = parse_json_arg(payload) or {}
	if not data.get("student") or not data.get("route"):
		return fail(
			message_en="Student and route are required.",
			message_ar="الطالب والخط مطلوبان.",
		)

	fields = {
		k: data.get(k)
		for k in ("student", "route", "stop", "active", "start_date", "end_date", "notes")
		if data.get(k) is not None
	}

	assignment_id = data.get("id") or data.get("name")
	if assignment_id:
		doc = frappe.get_doc("MS Transport Assignment", assignment_id)
		doc.update(fields)
		doc.save()
		msg_en, msg_ar = "Assignment updated.", "تم تحديث الإسناد."
	else:
		doc = frappe.get_doc({"doctype": "MS Transport Assignment", **fields})
		anchor_term(doc)
		doc.insert()
		msg_en, msg_ar = "Student assigned to route.", "تم إسناد الطالب للخط."

	frappe.db.commit()
	return {"success": True, "data": {"id": doc.name}, "message_en": msg_en, "message_ar": msg_ar}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY)
def delete_transport_assignment(assignment: str, persona: str = None):
	frappe.delete_doc("MS Transport Assignment", assignment, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": assignment},
		"message_en": "Assignment removed.",
		"message_ar": "تم إلغاء الإسناد.",
	}
