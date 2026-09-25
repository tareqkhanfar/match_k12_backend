# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Teaching material per subject.

A teacher uploads files or links a video against a subject, optionally
narrowed to one class. Students see the material for the subjects they are
actually taught, grouped by subject so the page reads as a library rather
than a flat list.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, today

from match_schools.api.utils import (
	apply_period,
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	paginate,
	parse_json_arg,
	resolve_scope,
)

TYPE_AR = {
	"Document": "مستند",
	"Video": "فيديو",
	"Presentation": "عرض تقديمي",
	"Worksheet": "ورقة عمل",
	"Link": "رابط",
	"Audio": "ملف صوتي",
	"Other": "أخرى",
}
TYPE_ICON = {
	"Document": "file-text",
	"Video": "video",
	"Presentation": "presentation",
	"Worksheet": "clipboard-list",
	"Link": "link",
	"Audio": "headphones",
	"Other": "file",
}


def _visible_filters(persona: str, scope: dict, student: str = None) -> dict | None:
	"""Which resources this persona may see. None means nothing.

	`student` narrows a family to one child's subjects.
	"""
	if persona in BACK_OFFICE:
		return {}

	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import courses_taught as courses_of_instructor

		courses = courses_of_instructor(scope.get("instructor"))
		return {"course": ["in", sorted(courses)]} if courses else None

	# Student or parent: the subjects their classes are taught.
	students = scope.get("students") or []
	if student:
		if student not in students:
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)
		students = [student]
	if not students:
		return None

	groups = [
		r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students], "parenttype": "Student Group", "active": 1},
			fields=["parent"],
		)
	]
	if not groups:
		return None

	courses = {
		r.course
		for r in frappe.get_all(
			"Course Schedule",
			filters={"student_group": ["in", groups]},
			fields=["course"],
			limit=2000,
		)
		if r.course
	}
	courses |= {
		r.course
		for r in frappe.get_all(
			"Student Group", filters={"name": ["in", groups]}, fields=["course"]
		)
		if r.course
	}
	if not courses:
		return None

	# Only published material, and either school-wide or aimed at their class.
	return {
		"course": ["in", sorted(courses)],
		"status": "Published",
		"student_group": ["in", [""] + groups + [None]],
	}


def _may_change(persona: str, instructor: str | None, scope: dict | None = None) -> bool:
	"""The office may change any resource; a teacher only what they published."""
	if persona in BACK_OFFICE:
		return True
	if persona != ROLE_TEACHER:
		return False
	mine = (scope or resolve_scope(persona)).get("instructor")
	return bool(mine) and instructor == mine


def _assert_may_change(persona: str, resource: str) -> None:
	instructor = frappe.db.get_value("MS Resource", resource, "instructor")
	if not _may_change(persona, instructor):
		frappe.throw(
			_("Only the teacher who published this may change it."), frappe.PermissionError
		)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_resources(
	course: str = None,
	resource_type: str = None,
	search: str = None,
	student: str = None,
	persona: str = None,
):
	"""Material grouped by subject."""
	scope = resolve_scope(persona)
	filters = _visible_filters(persona, scope, student)
	if filters is None:
		return {"subjects": [], "total": 0, "types": _type_list()}

	if course:
		filters["course"] = course
	if resource_type:
		filters["resource_type"] = resource_type
	if search:
		filters["title"] = ["like", f"%{search}%"]

	apply_period(filters, "MS Resource")

	rows = frappe.get_all(
		"MS Resource",
		filters=filters,
		fields=[
			"name", "title", "course", "student_group", "resource_type", "status",
			"description", "external_url", "instructor", "topic", "published_on",
			"view_count",
		],
		order_by="course, published_on desc",
		limit=500,
	)

	# One query for every attachment, rather than one per resource.
	files: dict[str, list] = {}
	if rows:
		for f in frappe.get_all(
			"MS Attachment",
			filters={
				"parent": ["in", [r.name for r in rows]],
				"parenttype": "MS Resource",
			},
			fields=["parent", "file_url", "file_name", "file_size"],
			order_by="idx",
		):
			files.setdefault(f.parent, []).append(
				{"file_url": f.file_url, "file_name": f.file_name, "file_size": f.file_size}
			)

	by_course: dict[str, list] = {}
	for r in rows:
		by_course.setdefault(r.course, []).append(
			{
				"id": r.name,
				"title": r.title,
				"course": r.course,
				"student_group": r.student_group,
				"type": r.resource_type,
				"type_label": TYPE_AR.get(r.resource_type, r.resource_type),
				"icon": TYPE_ICON.get(r.resource_type, "file"),
				"status": r.status,
				"description": r.description,
				"url": r.external_url,
				"instructor": r.instructor,
				"topic": r.topic,
				"published_on": str(r.published_on or ""),
				"views": cint(r.view_count),
				"files": files.get(r.name, []),
				# Said per row so a screen offers edit and delete only where
				# save_resource and delete_resource will allow them.
				"can_edit": _may_change(persona, r.instructor, scope),
			}
		)

	return {
		"subjects": [
			{"course": course_name, "count": len(items), "items": items}
			for course_name, items in sorted(by_course.items())
		],
		"total": len(rows),
		"types": _type_list(),
	}


def _type_list() -> list[dict]:
	return [
		{"code": c, "label": l, "icon": TYPE_ICON.get(c, "file")} for c, l in TYPE_AR.items()
	]


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_resource(payload: str | dict, persona: str = None):
	"""Publish teaching material."""
	from match_schools.api.assignments import _normalise_files

	data = parse_json_arg(payload) or {}
	if not data.get("title") or not data.get("course"):
		return fail(
			message_en="A title and a subject are required.",
			message_ar="العنوان والمادة مطلوبان.",
		)

	# A teacher may only publish under a subject they teach — in that class,
	# when the resource is aimed at one — and only edit what they published.
	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import assert_teacher_owns_course, assert_teacher_teaches

		assert_teacher_owns_course(persona, data["course"])
		if data.get("student_group"):
			assert_teacher_teaches(persona, data["student_group"], data["course"])
	if data.get("id"):
		_assert_may_change(persona, data["id"])

	doc = (
		frappe.get_doc("MS Resource", data["id"])
		if data.get("id")
		else frappe.new_doc("MS Resource")
	)

	for field in (
		"title", "course", "student_group", "resource_type", "status",
		"description", "external_url", "topic",
	):
		if data.get(field) is not None:
			setattr(doc, field, data[field])

	doc.academic_year = data.get("academic_year") or get_default_academic_year()
	doc.academic_term = data.get("academic_term") or get_default_academic_term()
	doc.published_on = data.get("published_on") or today()
	if persona == ROLE_TEACHER and not doc.instructor:
		doc.instructor = resolve_scope(persona).get("instructor")

	if data.get("files") is not None:
		doc.set("files", [])
		for a in _normalise_files(data["files"]):
			doc.append(
				"files",
				{
					"file_url": a["file_url"],
					"file_name": a.get("file_name"),
					"file_size": a.get("file_size") or 0,
					"uploaded_on": now_datetime(),
				},
			)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name, "title": doc.title},
		"message_en": "Resource saved.",
		"message_ar": "تم حفظ المصدر التعليمي.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_resource(resource: str, persona: str = None):
	# Teaching the subject is not enough: a colleague's material is theirs.
	_assert_may_change(persona, resource)

	frappe.delete_doc("MS Resource", resource, ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": resource},
		"message_en": "Deleted.",
		"message_ar": "تم حذف المصدر.",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def open_resource(resource: str, persona: str = None):
	"""Record that a student opened the material, so a teacher sees reach."""
	frappe.db.sql(
		"UPDATE `tabMS Resource` SET view_count = COALESCE(view_count, 0) + 1 WHERE name = %s",
		resource,
	)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": resource},
		"message_en": "",
		"message_ar": "",
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def resource_options(persona: str = None):
	"""Subjects and classes this persona may publish against."""
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		from match_schools.api.gradeflow import courses_taught as courses_of_instructor

		courses = sorted(courses_of_instructor(scope.get("instructor")))
	else:
		courses = frappe.get_all("Course", pluck="name", limit=300)

	return {
		"courses": courses,
		"types": _type_list(),
		"groups": [
			{"id": g.name, "name": g.student_group_name or g.name}
			for g in frappe.get_all(
				"Student Group",
				filters=apply_period({"disabled": 0}, "Student Group"),
				fields=["name", "student_group_name"],
				order_by="student_group_name",
				limit=300,
			)
		],
	}
