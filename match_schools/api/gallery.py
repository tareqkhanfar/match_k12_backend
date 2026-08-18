"""Photos of what a class actually did.

A school year is mostly invisible to the family: marks, attendance, and a
timetable. The trip, the science fair, the day the class planted a garden —
none of that reaches home except through a child's account of it. An album per
event, attached to the class, is the cheapest way to close that gap.

Albums belong to a class and to a term. A class is promoted every year and
"the first grade" means a different set of children each time, so an album
without a term becomes unattributable the moment a second year exists.

Visibility follows the same rule as lesson preparation: staff and the class's
own teachers manage albums; students and their families read the ones marked
published. An album being assembled on Tuesday for Sunday's trip is not an
announcement.
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate

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

# Images only. An album is a wall of pictures, and letting a PDF in means every
# viewer has to cope with a tile that cannot be shown.
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS_PER_ALBUM = 60


def _teacher_groups(persona: str) -> list[str]:
	"""The classes this teacher takes."""
	instructor = resolve_scope(persona).get("instructor")
	if not instructor:
		return []
	groups = set(
		frappe.get_all(
			"Student Group Instructor",
			filters={"instructor": instructor, "parenttype": "Student Group"},
			pluck="parent",
		)
	)
	groups |= {
		r.student_group
		for r in frappe.get_all(
			"Course Schedule",
			filters={"instructor": instructor, "docstatus": ["<", 2]},
			fields=["student_group"],
			limit_page_length=0,
		)
		if r.student_group
	}
	return sorted(groups)


def _family_groups(persona: str) -> list[str]:
	"""The classes this student sits in, or this parent's children sit in."""
	students = resolve_scope(persona).get("students") or []
	if not students:
		return []
	return sorted(
		{
			r.parent
			for r in frappe.get_all(
				"Student Group Student",
				filters={"student": ["in", students], "active": 1},
				fields=["parent"],
				limit_page_length=0,
			)
		}
	)


def visible_groups(persona: str) -> list[str] | None:
	"""Classes this caller may see albums for; None means no restriction."""
	if persona in BACK_OFFICE:
		return None
	if persona == ROLE_TEACHER:
		return _teacher_groups(persona)
	return _family_groups(persona)


def _may_manage(persona: str, student_group: str) -> bool:
	"""Who may add and edit an album: staff, and the class's own teachers."""
	if persona in BACK_OFFICE:
		return True
	if persona != ROLE_TEACHER:
		return False
	return student_group in _teacher_groups(persona)


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def list_albums(
	student_group: str = None,
	academic_year: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""Albums for a class, newest event first."""
	filters: dict = {}
	allowed = visible_groups(persona)

	if student_group:
		if allowed is not None and student_group not in allowed:
			frappe.throw(_("This class is not yours."), frappe.PermissionError)
		filters["student_group"] = student_group
	elif allowed is not None:
		if not allowed:
			return {"albums": [], "can_manage": False}
		filters["student_group"] = ["in", allowed]

	# A family never sees an album still being put together.
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		filters["is_published"] = 1

	if academic_year:
		filters["academic_year"] = academic_year
	if academic_term:
		filters["academic_term"] = academic_term

	rows = frappe.get_all(
		"MS Gallery Album",
		filters=filters,
		fields=[
			"name", "title", "student_group", "event_date", "description",
			"cover_image", "photo_count", "is_published", "academic_year",
			"academic_term", "created_by_user",
		],
		order_by="event_date desc, creation desc",
		limit_page_length=0,
	)

	names = {r.student_group for r in rows if r.student_group}
	labels = {}
	if names:
		labels = {
			g.name: g.student_group_name
			for g in frappe.get_all(
				"Student Group",
				filters={"name": ["in", list(names)]},
				fields=["name", "student_group_name"],
				limit_page_length=0,
			)
		}

	return {
		"albums": [
			{
				"id": r.name,
				"title": r.title,
				"student_group": r.student_group,
				"class_name": labels.get(r.student_group, r.student_group),
				"event_date": str(r.event_date or ""),
				"description": r.description,
				"cover_image": r.cover_image,
				"photo_count": cint(r.photo_count),
				"is_published": bool(cint(r.is_published)),
				"academic_year": r.academic_year,
				"academic_term": r.academic_term,
			}
			for r in rows
		],
		"can_manage": bool(student_group and _may_manage(persona, student_group)),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_album(album: str = None, persona: str = None):
	"""One album with all its photos."""
	if not album:
		return fail(message_en="An album is required.", message_ar="يجب تحديد الألبوم.")

	doc = frappe.get_doc("MS Gallery Album", album)
	allowed = visible_groups(persona)
	if allowed is not None and doc.student_group not in allowed:
		frappe.throw(_("This album is not yours."), frappe.PermissionError)
	if persona in (ROLE_STUDENT, ROLE_PARENT) and not cint(doc.is_published):
		frappe.throw(_("This album is not published."), frappe.PermissionError)

	group_name = frappe.db.get_value("Student Group", doc.student_group, "student_group_name")
	return {
		"id": doc.name,
		"title": doc.title,
		"student_group": doc.student_group,
		"class_name": group_name or doc.student_group,
		"event_date": str(doc.event_date or ""),
		"description": doc.description,
		"cover_image": doc.cover_image,
		"is_published": bool(cint(doc.is_published)),
		"academic_year": doc.academic_year,
		"academic_term": doc.academic_term,
		"can_manage": _may_manage(persona, doc.student_group),
		"photos": [
			{
				"file_url": p.file_url,
				"caption": p.caption,
				"file_name": p.file_name,
				"sort_order": cint(p.sort_order),
			}
			for p in sorted(doc.photos or [], key=lambda x: (cint(x.sort_order), x.idx))
		],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_album(payload: str | dict = None, persona: str = None):
	"""Create or update an album and its photos."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	album = data.get("album")
	student_group = data.get("student_group")

	if album:
		doc = frappe.get_doc("MS Gallery Album", album)
		student_group = doc.student_group
	else:
		if not student_group:
			return fail(message_en="A class is required.", message_ar="يجب تحديد الشعبة.")
		doc = frappe.new_doc("MS Gallery Album")
		doc.student_group = student_group

	if not _may_manage(persona, student_group):
		frappe.throw(_("This class is not yours."), frappe.PermissionError)

	if "title" in data:
		doc.title = (data.get("title") or "").strip()
	if "event_date" in data:
		doc.event_date = data.get("event_date")
	if "description" in data:
		doc.description = data.get("description")
	if "is_published" in data:
		doc.is_published = cint(data.get("is_published"))
	if "cover_image" in data:
		doc.cover_image = data.get("cover_image")

	if not doc.title:
		return fail(message_en="A title is required.", message_ar="عنوان الحدث مطلوب.")
	if not doc.event_date:
		return fail(message_en="An event date is required.", message_ar="تاريخ الحدث مطلوب.")

	photos = data.get("photos")
	if photos is not None:
		if len(photos) > MAX_PHOTOS_PER_ALBUM:
			return fail(
				message_en=f"An album holds at most {MAX_PHOTOS_PER_ALBUM} photos.",
				message_ar=f"الحد الأقصى {MAX_PHOTOS_PER_ALBUM} صورة في الألبوم الواحد.",
			)
		doc.set("photos", [])
		for i, p in enumerate(photos):
			url = (p or {}).get("file_url")
			if not url:
				continue
			doc.append(
				"photos",
				{
					"file_url": url,
					"caption": (p.get("caption") or "").strip(),
					"file_name": p.get("file_name"),
					"sort_order": cint(p.get("sort_order")) or i + 1,
				},
			)

	# The album belongs to the term its event happened in, not to today: a trip
	# entered in September that happened in June belongs to June.
	doc.academic_year = None
	doc.academic_term = None
	_stamp_term(doc)

	if not doc.created_by_user:
		doc.created_by_user = frappe.session.user
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "photo_count": cint(doc.photo_count)},
		"message_en": "Album saved.",
		"message_ar": "تم حفظ الألبوم.",
	}


def _stamp_term(doc) -> None:
	"""Anchor the album to the term its event date falls in."""
	when = doc.event_date
	row = []
	if when:
		row = frappe.db.sql(
			"""
			select name, academic_year from `tabAcademic Term`
			where term_start_date <= %(d)s and term_end_date >= %(d)s
			order by term_start_date desc limit 1
			""",
			{"d": getdate(when)},
			as_dict=True,
		)
	if row:
		doc.academic_term = row[0].name
		doc.academic_year = row[0].academic_year
	else:
		doc.academic_year = get_default_academic_year()
		doc.academic_term = get_default_academic_term()


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_album(album: str = None, persona: str = None):
	"""Remove an album and the photos stored for it."""
	if not album:
		return fail(message_en="An album is required.", message_ar="يجب تحديد الألبوم.")
	doc = frappe.get_doc("MS Gallery Album", album)
	if not _may_manage(persona, doc.student_group):
		frappe.throw(_("This class is not yours."), frappe.PermissionError)

	# The uploaded files outlive the album unless they are removed with it,
	# and an orphaned private file is invisible storage nobody can reclaim.
	for p in doc.photos or []:
		name = frappe.db.get_value("File", {"file_url": p.file_url}, "name")
		if name:
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)

	frappe.delete_doc("MS Gallery Album", album, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"deleted": album},
		"message_en": "Album deleted.",
		"message_ar": "تم حذف الألبوم.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def upload_photo(persona: str = None):
	"""Store one uploaded image and return its URL.

	Separate from the assignments uploader because the rules differ: images
	only, and a smaller ceiling — a phone photo is a few megabytes and forty of
	them in one album is already a large page to load.
	"""
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(message_en="No file received.", message_ar="لم يتم استلام أي ملف.")

	student_group = frappe.form_dict.get("student_group")
	if not student_group or not _may_manage(persona, student_group):
		frappe.throw(_("This class is not yours."), frappe.PermissionError)

	filename = uploaded.filename or "photo"
	extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	if extension not in ALLOWED_IMAGE_EXTENSIONS:
		return fail(
			message_en=f"File type .{extension} is not an image.",
			message_ar=f"نوع الملف .{extension} ليس صورة مسموحاً بها.",
		)

	content = uploaded.stream.read()
	if len(content) > MAX_PHOTO_BYTES:
		return fail(
			message_en="Photo is larger than 10 MB.",
			message_ar="حجم الصورة يتجاوز ١٠ ميجابايت.",
		)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			# Private: a class photo shows identifiable children and must not
			# be readable by anyone holding a guessed URL.
			"is_private": 1,
			"attached_to_doctype": "MS Gallery Album",
			"attached_to_name": frappe.form_dict.get("album") or None,
		}
	)
	file_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {
			"file_url": file_doc.file_url,
			"file_name": file_doc.file_name,
			"file_size": len(content),
		},
		"message_en": "Photo uploaded.",
		"message_ar": "تم رفع الصورة.",
	}
