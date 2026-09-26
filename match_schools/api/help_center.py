"""The help centre: how-to videos and questions with answers about the system.

Everyone who uses the portal reads it; each entry names the personas it is
written for, and an entry for nobody in particular is for everyone. Writing is
reserved for the Administrator account — this is the vendor's guidance on how
the system works, kept the same from school to school, not school content a
principal edits.
"""

import re

import frappe
from frappe.utils import cint

from match_schools.api.utils import (
	PERSONA_PRIORITY,
	fail,
	ms_endpoint,
	parse_json_arg,
)

DOCTYPE = "MS Help Article"
KINDS = ("Video", "Question")
VIDEO_EXT = r"\.(mp4|webm|mov|m4v|ogv)$"


def _may_edit() -> bool:
	return frappe.session.user == "Administrator"


def _assert_may_edit():
	if not _may_edit():
		frappe.throw("إضافة مواد المساعدة وتعديلها لحساب مدير النظام فقط.", frappe.PermissionError)


def _audience(raw: str | None) -> list[str]:
	return [p for p in re.split(r"[,\s]+", raw or "") if p in PERSONA_PRIORITY]


def _embed(url: str) -> str:
	"""A link someone pasted, as something a page can play in a frame.

	YouTube, Vimeo and Google Drive each hand out several shapes of link; the
	embeddable one is derived here so the person adding a video can paste
	whatever the browser's address bar shows.
	"""
	url = (url or "").strip()
	m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([\w-]{6,})", url)
	if m:
		return f"https://www.youtube.com/embed/{m[1]}"
	m = re.search(r"vimeo\.com/(?:video/)?(\d+)", url)
	if m:
		return f"https://player.vimeo.com/video/{m[1]}"
	m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=)([\w-]+)", url)
	if m:
		return f"https://drive.google.com/file/d/{m[1]}/preview"
	return ""


def _row(d) -> dict:
	return {
		"name": d.name,
		"title": d.title,
		"kind": d.kind or "Question",
		"topic": d.topic or "",
		"audience": _audience(d.audience),
		"body": d.body or "",
		"videoFile": d.video_file or "",
		"videoUrl": d.video_url or "",
		"embedUrl": _embed(d.video_url) if d.video_url else "",
		"isPublished": cint(d.is_published),
		"sortOrder": cint(d.sort_order),
		"views": cint(d.views),
		"modified": str(d.modified),
	}


FIELDS = [
	"name", "title", "kind", "topic", "audience", "body", "video_file", "video_url",
	"is_published", "sort_order", "views", "modified",
]


@frappe.whitelist()
@ms_endpoint(*PERSONA_PRIORITY)
def list_articles(persona: str = None):
	"""Everything this persona may read, grouped by topic on the screen.

	The Administrator sees every entry, drafts and all, so they can be
	reviewed before they go out.
	"""
	editor = _may_edit()
	filters = {} if editor else {"is_published": 1}
	rows = frappe.get_all(
		DOCTYPE, filters=filters, fields=FIELDS, order_by="sort_order asc, modified desc", limit_page_length=0
	)
	out = []
	for d in rows:
		audience = _audience(d.audience)
		if not editor and audience and persona not in audience:
			continue
		out.append(_row(d))
	return {"articles": out, "canEdit": editor, "persona": persona}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*PERSONA_PRIORITY)
def mark_viewed(name: str, persona: str = None):
	"""Count a view — which videos people actually open is worth knowing."""
	if frappe.db.exists(DOCTYPE, name) and not _may_edit():
		frappe.db.sql(f"update `tab{DOCTYPE}` set views = coalesce(views, 0) + 1 where name = %s", name)
		frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*PERSONA_PRIORITY)
def save_article(payload: str | dict = None, persona: str = None):
	_assert_may_edit()
	data = parse_json_arg(payload) or {}
	title = (data.get("title") or "").strip()
	if not title:
		return fail("A title is required.", "اكتب العنوان أو السؤال.")
	kind = data.get("kind") if data.get("kind") in KINDS else "Question"
	video_file = (data.get("videoFile") or "").strip()
	video_url = (data.get("videoUrl") or "").strip()
	body = data.get("body") or ""
	if kind == "Video" and not (video_file or video_url):
		return fail("A video is required.", "ارفع ملف الفيديو أو الصق رابطه.")
	if video_url and not _embed(video_url):
		return fail(
			"Unsupported video link.",
			"رابط الفيديو غير مدعوم — استخدم رابطاً من YouTube أو Vimeo أو Google Drive.",
		)
	if kind == "Question" and not body.strip():
		return fail("An answer is required.", "اكتب الجواب.")

	doc = frappe.get_doc(DOCTYPE, data["name"]) if data.get("name") else frappe.new_doc(DOCTYPE)
	doc.title = title[:140]
	doc.kind = kind
	doc.topic = (data.get("topic") or "").strip()[:140]
	doc.audience = ",".join(p for p in data.get("audience") or [] if p in PERSONA_PRIORITY)
	doc.body = body
	doc.video_file = video_file
	doc.video_url = video_url
	doc.is_published = 1 if data.get("isPublished", 1) else 0
	doc.sort_order = cint(data.get("sortOrder"))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"article": _row(doc), "message_ar": "تم الحفظ."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*PERSONA_PRIORITY)
def delete_article(name: str, persona: str = None):
	_assert_may_edit()
	if frappe.db.exists(DOCTYPE, name):
		frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
		frappe.db.commit()
	return {"message_ar": "تم الحذف."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*PERSONA_PRIORITY)
def upload_video(persona: str = None):
	"""A video file for an entry. Public, so it plays for every reader without
	a per-file permission check; nothing in the help centre is confidential."""
	_assert_may_edit()
	upload = frappe.request.files.get("file") if frappe.request else None
	if not upload:
		return fail("No file.", "اختر ملف الفيديو.")
	name = upload.filename or "video.mp4"
	if not re.search(VIDEO_EXT, name, re.I):
		return fail("Not a video.", "الملف يجب أن يكون فيديو (MP4 أو WebM أو MOV).")
	f = frappe.get_doc(
		{"doctype": "File", "file_name": name, "content": upload.stream.read(), "is_private": 0, "folder": "Home"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"url": f.file_url}
