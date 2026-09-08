"""ملفاتي — a teacher's own file store.

Teachers already keep worksheets, slides and past papers somewhere; usually a
memory stick and an email to themselves. This is that, inside the system: a
folder tree, uploads, and three ways of letting other people at a file —
shared with colleagues, published to a class, or attached to a print request
without uploading it a second time.

Two rules shape everything below. A file belongs to exactly one person, and
sharing never transfers it: a colleague reads, the owner decides. And the
store is not a backup — there is a quota, enforced on upload rather than
discovered when the disk fills.
"""

import frappe
from frappe import _
from frappe.utils import cint

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	apply_period,
	fail,
	ms_endpoint,
	resolve_scope,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
ALL_ROLES = (*STAFF, ROLE_STUDENT, ROLE_PARENT)

MAX_FILE_BYTES = 25 * 1024 * 1024
QUOTA_BYTES = 2 * 1024 * 1024 * 1024
MAX_DEPTH = 6
MAX_FOLDERS = 300
MAX_FILES = 3000

ALLOWED_EXTENSIONS = {
	"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt", "odp", "ods",
	"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp",
	"mp3", "wav", "m4a", "mp4", "webm", "mov",
	"zip", "rar", "7z",
}

# What the screen shows as an icon, so it does not have to know every
# extension. Anything unlisted is a plain document.
KIND_BY_EXTENSION = {
	**{e: "image" for e in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp")},
	**{e: "audio" for e in ("mp3", "wav", "m4a")},
	**{e: "video" for e in ("mp4", "webm", "mov")},
	**{e: "archive" for e in ("zip", "rar", "7z")},
	**{e: "sheet" for e in ("xls", "xlsx", "csv", "ods")},
	**{e: "slides" for e in ("ppt", "pptx", "odp")},
	"pdf": "pdf",
}


def _at(folder: str | None):
	"""A filter for "inside this folder", where the root is both NULL and "".

	Frappe writes an unset Link as NULL, so a plain `= ""` matches nothing and
	the root of the drive lists as empty however many files are in it. `IN
	("", NULL)` does not help either — in SQL nothing equals NULL — so the
	root is expressed as "not set", which Frappe renders as an ifnull check.
	"""
	return folder if folder else ["is", "not set"]


def _extension(name: str) -> str:
	return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def _kind(name: str) -> str:
	return KIND_BY_EXTENSION.get(_extension(name), "document")


def _owner_of(persona: str) -> str:
	return frappe.session.user


def _assert_own_folder(folder: str | None) -> None:
	"""A folder the caller does not own is not a folder they may write into."""
	if not folder:
		return
	owner = frappe.db.get_value("MS Drive Folder", folder, "owner_user")
	if not owner:
		frappe.throw(_("Folder not found."), frappe.DoesNotExistError)
	if owner != frappe.session.user:
		frappe.throw(_("This folder is not yours."), frappe.PermissionError)


def _breadcrumb(folder: str | None) -> list[dict]:
	trail: list[dict] = []
	seen: set[str] = set()
	cursor = folder
	while cursor and cursor not in seen:
		seen.add(cursor)
		row = frappe.db.get_value(
			"MS Drive Folder", cursor, ["name", "title", "parent_folder"], as_dict=True
		)
		if not row:
			break
		trail.append({"id": row.name, "title": row.title})
		cursor = row.parent_folder
	trail.reverse()
	return trail


def _usage(user: str) -> dict:
	used = (
		frappe.db.sql(
			"select ifnull(sum(file_size), 0) from `tabMS Drive File` where owner_user = %s",
			user,
		)[0][0]
		or 0
	)
	return {
		"used": int(used),
		"quota": QUOTA_BYTES,
		"percent": round(int(used) * 100 / QUOTA_BYTES, 1) if QUOTA_BYTES else 0,
		"files": frappe.db.count("MS Drive File", {"owner_user": user}),
		"folders": frappe.db.count("MS Drive Folder", {"owner_user": user}),
	}


def _file_row(r: dict, mine: bool) -> dict:
	return {
		"id": r["name"],
		"title": r["title"],
		"file_url": r["file_url"],
		"file_name": r.get("file_name"),
		"file_size": cint(r.get("file_size")),
		"kind": _kind(r.get("file_name") or r["title"]),
		"extension": _extension(r.get("file_name") or r["title"]),
		"folder": r.get("folder"),
		"description": r.get("description"),
		"shared_with_staff": bool(cint(r.get("shared_with_staff"))),
		"is_published": bool(cint(r.get("is_published"))),
		"student_group": r.get("student_group"),
		"course": r.get("course"),
		"download_count": cint(r.get("download_count")),
		"modified": str(r.get("modified") or ""),
		"owner_user": r.get("owner_user"),
		"mine": mine,
	}


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*STAFF)
def list_items(folder: str = None, search: str = None, persona: str = None):
	"""One folder of the caller's drive, or a search across all of it.

	A search ignores the folder: someone looking for "امتحان الفصل" wants it
	wherever they filed it, which is the whole point of searching.
	"""
	user = _owner_of(persona)
	if folder:
		_assert_own_folder(folder)

	if search:
		needle = f"%{search.strip()}%"
		files = frappe.get_all(
			"MS Drive File",
			filters={"owner_user": user, "title": ["like", needle]},
			fields=[
				"name", "title", "file_url", "file_name", "file_size", "folder",
				"description", "shared_with_staff", "is_published", "student_group",
				"course", "download_count", "modified", "owner_user",
			],
			order_by="modified desc",
			limit_page_length=200,
		)
		folders = frappe.get_all(
			"MS Drive Folder",
			filters={"owner_user": user, "title": ["like", needle]},
			fields=["name", "title", "parent_folder", "colour", "modified"],
			order_by="title",
			limit_page_length=100,
		)
	else:
		files = frappe.get_all(
			"MS Drive File",
			filters={"owner_user": user, "folder": _at(folder)},
			fields=[
				"name", "title", "file_url", "file_name", "file_size", "folder",
				"description", "shared_with_staff", "is_published", "student_group",
				"course", "download_count", "modified", "owner_user",
			],
			order_by="title",
			limit_page_length=0,
		)
		folders = frappe.get_all(
			"MS Drive Folder",
			filters={"owner_user": user, "parent_folder": _at(folder)},
			fields=["name", "title", "parent_folder", "colour", "modified"],
			order_by="title",
			limit_page_length=0,
		)

	# What each folder holds, so the screen can say "٤ ملفات" without asking
	# once per row.
	ids = [f.name for f in folders]
	child_files: dict[str, int] = {}
	child_folders: dict[str, int] = {}
	if ids:
		for row in frappe.db.sql(
			"""select folder, count(*) as n from `tabMS Drive File`
			    where folder in %(ids)s group by folder""",
			{"ids": ids},
			as_dict=True,
		):
			child_files[row.folder] = row.n
		for row in frappe.db.sql(
			"""select parent_folder, count(*) as n from `tabMS Drive Folder`
			    where parent_folder in %(ids)s group by parent_folder""",
			{"ids": ids},
			as_dict=True,
		):
			child_folders[row.parent_folder] = row.n

	return {
		"folder": folder,
		"breadcrumb": _breadcrumb(folder),
		"folders": [
			{
				"id": f.name,
				"title": f.title,
				"parent_folder": f.parent_folder,
				"colour": f.colour,
				"file_count": child_files.get(f.name, 0),
				"folder_count": child_folders.get(f.name, 0),
				"modified": str(f.modified or ""),
			}
			for f in folders
		],
		"files": [_file_row(dict(f), True) for f in files],
		"usage": _usage(user),
		"searching": bool(search),
	}


@frappe.whitelist()
@ms_endpoint(*STAFF)
def folder_tree(persona: str = None):
	"""Every folder the caller owns, for the move dialog."""
	user = _owner_of(persona)
	rows = frappe.get_all(
		"MS Drive Folder",
		filters={"owner_user": user},
		fields=["name", "title", "parent_folder"],
		order_by="title",
		limit_page_length=0,
	)
	return {
		"folders": [
			{"id": r.name, "title": r.title, "parent_folder": r.parent_folder} for r in rows
		]
	}


@frappe.whitelist()
@ms_endpoint(*STAFF)
def shared_with_me(search: str = None, persona: str = None):
	"""Files colleagues have shared, read-only."""
	filters: dict = {"shared_with_staff": 1, "owner_user": ["!=", frappe.session.user]}
	if search:
		filters["title"] = ["like", f"%{search.strip()}%"]
	apply_period(filters, "MS Drive File")

	rows = frappe.get_all(
		"MS Drive File",
		filters=filters,
		fields=[
			"name", "title", "file_url", "file_name", "file_size", "folder",
			"description", "shared_with_staff", "is_published", "student_group",
			"course", "download_count", "modified", "owner_user",
		],
		order_by="modified desc",
		limit_page_length=200,
	)
	owners = {r.owner_user for r in rows}
	names = (
		{
			u.name: u.full_name
			for u in frappe.get_all(
				"User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"]
			)
		}
		if owners
		else {}
	)
	return {
		"files": [
			{**_file_row(dict(r), False), "owner_name": names.get(r.owner_user) or r.owner_user}
			for r in rows
		]
	}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def create_folder(title: str = None, parent_folder: str = None, persona: str = None):
	"""Add a folder inside another the caller owns."""
	user = _owner_of(persona)
	title = (title or "").strip()
	if not title:
		return fail(message_en="A name is required.", message_ar="اسم المجلد مطلوب.")

	_assert_own_folder(parent_folder)
	if frappe.db.count("MS Drive Folder", {"owner_user": user}) >= MAX_FOLDERS:
		return fail(
			message_en=f"At most {MAX_FOLDERS} folders.",
			message_ar=f"الحد الأقصى {MAX_FOLDERS} مجلداً.",
		)

	depth = len(_breadcrumb(parent_folder))
	if depth >= MAX_DEPTH:
		return fail(
			message_en=f"Folders nest {MAX_DEPTH} deep at most.",
			message_ar=f"لا يمكن تداخل المجلدات أكثر من {MAX_DEPTH} مستويات.",
		)

	if frappe.db.exists(
		"MS Drive Folder",
		{"owner_user": user, "parent_folder": _at(parent_folder), "title": title},
	):
		return fail(
			message_en="A folder with that name is already here.",
			message_ar="يوجد مجلد بهذا الاسم في نفس المكان.",
		)

	doc = frappe.get_doc(
		{
			"doctype": "MS Drive Folder",
			"title": title[:140],
			"owner_user": user,
			"parent_folder": parent_folder,
			"depth": depth,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Folder created.",
		"message_ar": "تم إنشاء المجلد.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def upload(persona: str = None):
	"""Store one file in the caller's drive."""
	user = _owner_of(persona)
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(message_en="No file received.", message_ar="لم يتم استلام أي ملف.")

	folder = frappe.form_dict.get("folder") or None
	_assert_own_folder(folder)

	filename = uploaded.filename or "file"
	extension = _extension(filename)
	if extension not in ALLOWED_EXTENSIONS:
		return fail(
			message_en=f"File type .{extension} is not allowed.",
			message_ar=f"نوع الملف .{extension} غير مسموح.",
		)

	content = uploaded.stream.read()
	size = len(content)
	if size > MAX_FILE_BYTES:
		return fail(
			message_en="File is larger than 25 MB.",
			message_ar="حجم الملف يتجاوز ٢٥ ميجابايت.",
		)

	usage = _usage(user)
	if usage["used"] + size > QUOTA_BYTES:
		remaining = max(QUOTA_BYTES - usage["used"], 0) // (1024 * 1024)
		return fail(
			message_en="Not enough space left in your drive.",
			message_ar=f"لا توجد مساحة كافية — المتبقي {remaining} ميجابايت.",
		)
	if usage["files"] >= MAX_FILES:
		return fail(
			message_en=f"At most {MAX_FILES} files.",
			message_ar=f"الحد الأقصى {MAX_FILES} ملف.",
		)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			# Private by default: a teacher's own store is not a public one,
			# and publishing to a class is a deliberate act below.
			"is_private": 1,
		}
	)
	file_doc.save(ignore_permissions=True)

	doc = frappe.get_doc(
		{
			"doctype": "MS Drive File",
			"title": (frappe.form_dict.get("title") or filename)[:140],
			"owner_user": user,
			"folder": folder,
			"file_url": file_doc.file_url,
			"file_name": file_doc.file_name,
			"file_size": size,
			"file_type": extension,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": _file_row(
			{
				"name": doc.name,
				"title": doc.title,
				"file_url": doc.file_url,
				"file_name": doc.file_name,
				"file_size": size,
				"folder": folder,
				"owner_user": user,
			},
			True,
		),
		"message_en": "File uploaded.",
		"message_ar": "تم رفع الملف.",
	}


def _own_file(name: str):
	doc = frappe.get_doc("MS Drive File", name)
	if doc.owner_user != frappe.session.user:
		frappe.throw(_("This file is not yours."), frappe.PermissionError)
	return doc


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def rename_item(item: str = None, kind: str = "file", title: str = None, persona: str = None):
	"""Rename a file or a folder the caller owns."""
	title = (title or "").strip()
	if not (item and title):
		return fail(message_en="A name is required.", message_ar="الاسم مطلوب.")

	if kind == "folder":
		_assert_own_folder(item)
		frappe.db.set_value("MS Drive Folder", item, "title", title[:140])
	else:
		doc = _own_file(item)
		doc.db_set("title", title[:140])
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": item},
		"message_en": "Renamed.",
		"message_ar": "تم تغيير الاسم.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def move_item(item: str = None, kind: str = "file", folder: str = None, persona: str = None):
	"""Move a file or folder into another folder."""
	if not item:
		return fail(message_en="Nothing to move.", message_ar="لا يوجد ما يُنقل.")
	_assert_own_folder(folder)

	if kind == "folder":
		_assert_own_folder(item)
		if folder == item:
			return fail(
				message_en="A folder cannot contain itself.",
				message_ar="لا يمكن نقل المجلد إلى داخل نفسه.",
			)
		# Moving a folder inside its own descendant would cut the branch off
		# the tree: it would still exist, reachable from nothing.
		if folder and item in {b["id"] for b in _breadcrumb(folder)}:
			return fail(
				message_en="A folder cannot move inside itself.",
				message_ar="لا يمكن نقل المجلد إلى داخل أحد مجلداته الفرعية.",
			)
		if len(_breadcrumb(folder)) >= MAX_DEPTH:
			return fail(
				message_en="That would nest too deep.",
				message_ar="التداخل سيتجاوز الحد المسموح.",
			)
		frappe.db.set_value("MS Drive Folder", item, "parent_folder", folder)
	else:
		doc = _own_file(item)
		doc.db_set("folder", folder)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": item},
		"message_en": "Moved.",
		"message_ar": "تم النقل.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def delete_item(item: str = None, kind: str = "file", persona: str = None):
	"""Delete a file, or an empty folder.

	A folder with anything in it is refused rather than emptied: deleting
	forty files because someone clicked the wrong row is not recoverable here.
	"""
	if not item:
		return fail(message_en="Nothing to delete.", message_ar="لا يوجد ما يُحذف.")

	if kind == "folder":
		_assert_own_folder(item)
		inside = frappe.db.count("MS Drive File", {"folder": item}) + frappe.db.count(
			"MS Drive Folder", {"parent_folder": item}
		)
		if inside:
			return fail(
				message_en="The folder is not empty.",
				message_ar=f"المجلد غير فارغ — يحتوي {inside} عنصراً.",
			)
		frappe.delete_doc("MS Drive Folder", item, ignore_permissions=True, force=True)
	else:
		doc = _own_file(item)
		url = doc.file_url
		frappe.delete_doc("MS Drive File", item, ignore_permissions=True, force=True)
		# The bytes go with the row. Nothing else points at them: an upload
		# always creates its own File.
		for f in frappe.get_all("File", filters={"file_url": url}, pluck="name"):
			frappe.delete_doc("File", f, ignore_permissions=True, force=True)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": item},
		"message_en": "Deleted.",
		"message_ar": "تم الحذف.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def set_sharing(payload: str | dict = None, persona: str = None):
	"""Decide who else may see one file."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	doc = _own_file(data.get("file"))

	group = data.get("student_group")
	if group and persona == ROLE_TEACHER:
		# A teacher publishes to their own classes only.
		mine = resolve_scope(persona).get("student_groups") or []
		if mine and group not in mine:
			frappe.throw(_("This class is not yours."), frappe.PermissionError)

	doc.shared_with_staff = 1 if cint(data.get("shared_with_staff")) else 0
	doc.is_published = 1 if cint(data.get("is_published")) else 0
	doc.student_group = group
	doc.course = data.get("course")
	if "description" in data:
		doc.description = data.get("description")

	if doc.is_published and not doc.student_group:
		return fail(
			message_en="Choose the class to publish to.",
			message_ar="اختر الشعبة التي سيُنشر لها الملف.",
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Sharing updated.",
		"message_ar": "تم تحديث المشاركة.",
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def class_files(student_group: str = None, persona: str = None):
	"""Files published to a class, for the people in it.

	This is the one door students and guardians have into the drive, and it
	opens on published files for their own class only.
	"""
	mine = None
	if persona in (ROLE_STUDENT, ROLE_PARENT):
		students = resolve_scope(persona).get("students") or []
		mine = frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students or [""]], "active": 1},
			pluck="parent",
		)
		if student_group and student_group not in mine:
			frappe.throw(_("This class is not yours."), frappe.PermissionError)

	# A pupil or guardian who names no class means their own — the server
	# already knows which. Requiring them to name it forced the app to ask a
	# staff-only endpoint for the id first, which answered 403 and left the
	# screen empty for exactly the people it was written for.
	if not student_group:
		if mine is None:
			return {"files": []}
		if not mine:
			return {"files": []}
		filters = {"student_group": ["in", list(set(mine))], "is_published": 1}
	else:
		filters = {"student_group": student_group, "is_published": 1}
	apply_period(filters, "MS Drive File")
	rows = frappe.get_all(
		"MS Drive File",
		filters=filters,
		fields=[
			"name", "title", "file_url", "file_name", "file_size", "folder",
			"description", "shared_with_staff", "is_published", "student_group",
			"course", "download_count", "modified", "owner_user",
		],
		order_by="modified desc",
		limit_page_length=200,
	)
	owners = {r.owner_user for r in rows}
	names = (
		{
			u.name: u.full_name
			for u in frappe.get_all(
				"User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"]
			)
		}
		if owners
		else {}
	)
	return {
		"files": [
			{
				**_file_row(dict(r), r.owner_user == frappe.session.user),
				"owner_name": names.get(r.owner_user) or r.owner_user,
			}
			for r in rows
		]
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def record_download(file: str = None, persona: str = None):
	"""Count one download, so a teacher can see what is actually being used."""
	if not file or not frappe.db.exists("MS Drive File", file):
		return fail(message_en="File not found.", message_ar="الملف غير موجود.")
	frappe.db.sql(
		"update `tabMS Drive File` set download_count = ifnull(download_count, 0) + 1 "
		"where name = %s",
		file,
	)
	frappe.db.commit()
	return {"success": True, "data": {}, "message_en": "", "message_ar": ""}
