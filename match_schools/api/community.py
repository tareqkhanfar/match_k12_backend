"""The school's feed: achievements, activities, and what people say about them.

A school year reaches a family as marks and attendance. The prize a child won,
the trip the class took, the project the room spent a fortnight on — those
arrive, if at all, as a story over dinner. A feed the family can see, react to
and reply on is the difference between a portal they check and one they visit.

Visibility is the whole design. Three audiences, narrowing:

- School: everyone signed in.
- Class: the students of that class and their guardians.
- Student: that child and their guardians, and nobody else.

A "Student" post naming a child is visible to that family alone. Getting this
wrong would publish one child's business to another family, which is worse
than not having the feature.

Staff write; families read, like and comment. Comments are attributed and can
be hidden by staff rather than deleted, so a moderation decision leaves a
record instead of a hole.
"""

import frappe
from frappe import _
from frappe.utils import cint, now

from match_schools.api.utils import (
	fail,
	get_default_academic_term,
	get_default_academic_year,
	instructor_groups,
	ms_endpoint,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)

# The channel that holds everything not tied to a subject: school notices,
# trips, results. Named rather than empty so the client can ask for it.
GENERAL_CHANNEL = "__general__"
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS = 10
MAX_COMMENT_CHARS = 1000

TYPE_AR = {
	"Achievement": "إنجاز",
	"Activity": "نشاط",
	"Announcement": "إعلان",
	"General": "عام",
}
AUDIENCE_AR = {"School": "المدرسة كاملة", "Class": "شعبة محدّدة", "Student": "طالب محدّد"}


def _my_groups(persona: str) -> list[str]:
	"""Classes this caller belongs to — taught, attended, or via a child."""
	scope = resolve_scope(persona)
	if persona == ROLE_TEACHER:
		return instructor_groups(scope.get("instructor"))

	students = scope.get("students") or []
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


def _visible_filter(persona: str) -> list | None:
	"""An `or_filters` list describing what this caller may see.

	None means no restriction. Staff who are not teachers see everything;
	a teacher sees the school feed plus their own classes; a family sees the
	school feed, their classes, and posts about their own children.
	"""
	if persona in BACK_OFFICE:
		return None

	groups = _my_groups(persona)
	clauses: list[list] = [["audience", "=", "School"]]
	if groups:
		clauses.append(["student_group", "in", groups])

	if persona in (ROLE_STUDENT, ROLE_PARENT):
		students = resolve_scope(persona).get("students") or []
		if students:
			clauses.append(["student", "in", students])
	elif persona == ROLE_TEACHER:
		# A teacher also sees what they wrote, wherever it was aimed.
		clauses.append(["author", "=", frappe.session.user])

	return clauses


def _may_see(persona: str, doc) -> bool:
	"""Whether one post is visible to this caller."""
	if persona in BACK_OFFICE:
		return True
	if doc.author == frappe.session.user:
		return True
	if not cint(doc.is_published):
		return False
	if doc.audience == "School":
		return True
	if doc.audience == "Class":
		return doc.student_group in _my_groups(persona)
	if doc.audience == "Student":
		if persona == ROLE_TEACHER:
			# A teacher sees a child's post only through the class they teach.
			return bool(
				frappe.db.exists(
					"Student Group Student",
					{
						"student": doc.student,
						"parent": ["in", _my_groups(persona) or [""]],
						"active": 1,
					},
				)
			)
		return doc.student in (resolve_scope(persona).get("students") or [])
	return False


def _may_edit(persona: str, doc) -> bool:
	if persona in BACK_OFFICE:
		return True
	return persona == ROLE_TEACHER and doc.author == frappe.session.user


def _post_row(doc, persona: str, liked: set[str] | None = None) -> dict:
	return {
		"id": doc.name,
		"title": doc.title,
		"body": doc.body,
		"post_type": doc.post_type,
		"type_label": TYPE_AR.get(doc.post_type, doc.post_type),
		"audience": doc.audience,
		"audience_label": AUDIENCE_AR.get(doc.audience, doc.audience),
		"student_group": doc.student_group,
		"course": doc.get("course"),
		"course_name": (
			frappe.db.get_value("Course", doc.get("course"), "course_name")
			if doc.get("course")
			else None
		),
		"class_name": (
			frappe.db.get_value("Student Group", doc.student_group, "student_group_name")
			if doc.student_group
			else None
		),
		"student": doc.student,
		"student_name": (
			frappe.db.get_value("Student", doc.student, "student_name") if doc.student else None
		),
		"author_name": doc.author_name,
		"posted_on": str(doc.posted_on or ""),
		"is_published": bool(cint(doc.is_published)),
		"allow_comments": bool(cint(doc.allow_comments)),
		"pinned": bool(cint(doc.pinned)),
		"like_count": cint(doc.like_count),
		"comment_count": cint(doc.comment_count),
		"liked_by_me": doc.name in (liked or set()),
		"can_edit": _may_edit(persona, doc),
		"photos": [
			{"file_url": p.file_url, "caption": p.caption} for p in (doc.photos or [])
		],
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def feed(
	student_group: str = None,
	course: str = None,
	limit: int = 30,
	persona: str = None,
):
	"""The posts this caller may see, pinned first, then newest.

	`course` narrows the feed to one subject. A pupil takes eight subjects and
	each has its own stream of work and notices; one undifferentiated wall
	means the maths post scrolls past while they are looking for it. Passing
	`__general__` asks for the posts that belong to no subject — school
	notices, trips, results — which is the other half of the same problem.
	"""
	filters: dict = {}
	if persona not in BACK_OFFICE:
		filters["is_published"] = 1
	if student_group:
		filters["student_group"] = student_group
	if course == GENERAL_CHANNEL:
		filters["course"] = ["in", ["", None]]
	elif course:
		filters["course"] = course

	or_filters = _visible_filter(persona)
	names = frappe.get_all(
		"MS Community Post",
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="pinned desc, posted_on desc",
		limit_page_length=min(max(cint(limit) or 30, 1), 100),
	)
	if not names:
		return {"posts": [], "can_post": persona in STAFF}

	# Which of these the caller has already liked, in one query rather than
	# one per post.
	liked = {
		r.post
		for r in frappe.get_all(
			"MS Post Like",
			filters={"post": ["in", names], "user": frappe.session.user},
			fields=["post"],
			limit_page_length=0,
		)
	}

	posts = []
	for n in names:
		doc = frappe.get_doc("MS Community Post", n)
		# or_filters narrows the query; this is the authority. A Student post
		# must never reach another family because a filter was imprecise.
		if not _may_see(persona, doc):
			continue
		posts.append(_post_row(doc, persona, liked))

	return {"posts": posts, "can_post": persona in STAFF}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def get_post(post: str = None, persona: str = None):
	"""One post with its comments."""
	if not post:
		return fail(message_en="A post is required.", message_ar="يجب تحديد المنشور.")
	doc = frappe.get_doc("MS Community Post", post)
	if not _may_see(persona, doc):
		frappe.throw(_("This post is not for you."), frappe.PermissionError)

	liked = {
		r.post
		for r in frappe.get_all(
			"MS Post Like",
			filters={"post": post, "user": frappe.session.user},
			fields=["post"],
		)
	}
	row = _post_row(doc, persona, liked)

	comment_filters: dict = {"post": post}
	if persona not in STAFF:
		# A hidden comment stays on record for staff and disappears for
		# everyone else.
		comment_filters["is_hidden"] = 0
	comments = frappe.get_all(
		"MS Post Comment",
		filters=comment_filters,
		fields=[
			"name", "body", "author", "author_name", "posted_on",
			"parent_comment", "is_hidden",
		],
		order_by="posted_on asc",
		limit_page_length=0,
	)
	row["comments"] = [
		{
			"id": c.name,
			"body": c.body,
			"author": c.author,
			"author_name": c.author_name,
			"posted_on": str(c.posted_on or ""),
			"parent_comment": c.parent_comment,
			"is_hidden": bool(cint(c.is_hidden)),
			"can_delete": persona in STAFF or c.author == frappe.session.user,
			"can_hide": persona in STAFF,
		}
		for c in comments
	]
	return row


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def save_post(payload: str | dict = None, persona: str = None):
	"""Create or update a post."""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	post = data.get("post")

	if post:
		doc = frappe.get_doc("MS Community Post", post)
		if not _may_edit(persona, doc):
			frappe.throw(_("This post is not yours."), frappe.PermissionError)
	else:
		doc = frappe.new_doc("MS Community Post")
		doc.author = frappe.session.user
		doc.author_name = frappe.db.get_value("User", frappe.session.user, "full_name")
		doc.posted_on = now()

	for field in ("title", "body", "post_type", "audience",
	              "student_group", "student", "program", "course"):
		if field in data:
			doc.set(field, data.get(field))
	for flag in ("is_published", "allow_comments", "pinned"):
		if flag in data:
			doc.set(flag, cint(data.get(flag)))

	if not (doc.title or "").strip():
		return fail(message_en="A title is required.", message_ar="عنوان المنشور مطلوب.")

	# A teacher may only post to a class they teach, or about a student in one.
	# Without this a teacher could address the whole school, or another
	# teacher's class, from a request the screen never offers.
	if persona == ROLE_TEACHER:
		groups = _my_groups(persona)
		if doc.audience == "Class" and doc.student_group not in groups:
			frappe.throw(_("This class is not yours."), frappe.PermissionError)
		if doc.audience == "Student":
			if not frappe.db.exists(
				"Student Group Student",
				{"student": doc.student, "parent": ["in", groups or [""]], "active": 1},
			):
				frappe.throw(_("This student is not in your class."), frappe.PermissionError)
		if doc.audience == "School":
			frappe.throw(
				_("Only the administration posts to the whole school."),
				frappe.PermissionError,
			)

	photos = data.get("photos")
	if photos is not None:
		if len(photos) > MAX_PHOTOS:
			return fail(
				message_en=f"At most {MAX_PHOTOS} photos per post.",
				message_ar=f"الحد الأقصى {MAX_PHOTOS} صور للمنشور الواحد.",
			)
		doc.set("photos", [])
		for p in photos:
			url = (p or {}).get("file_url")
			if url:
				doc.append("photos", {"file_url": url, "caption": (p.get("caption") or "").strip()})

	if not doc.academic_year:
		doc.academic_year = get_default_academic_year()
	if not doc.academic_term:
		doc.academic_term = get_default_academic_term()

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Post saved.",
		"message_ar": "تم حفظ المنشور.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def delete_post(post: str = None, persona: str = None):
	"""Remove a post, its reactions, and the photos uploaded for it."""
	if not post:
		return fail(message_en="A post is required.", message_ar="يجب تحديد المنشور.")
	doc = frappe.get_doc("MS Community Post", post)
	if not _may_edit(persona, doc):
		frappe.throw(_("This post is not yours."), frappe.PermissionError)

	for p in doc.photos or []:
		name = frappe.db.get_value("File", {"file_url": p.file_url}, "name")
		if name:
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	# Likes and comments point at the post; leaving them makes rows that refer
	# to something gone.
	for dt in ("MS Post Like", "MS Post Comment"):
		for n in frappe.get_all(dt, filters={"post": post}, pluck="name"):
			frappe.delete_doc(dt, n, ignore_permissions=True, force=True)

	frappe.delete_doc("MS Community Post", post, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"deleted": post},
		"message_en": "Post deleted.",
		"message_ar": "تم حذف المنشور.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def toggle_like(post: str = None, persona: str = None):
	"""Like a post, or take the like back."""
	if not post:
		return fail(message_en="A post is required.", message_ar="يجب تحديد المنشور.")
	doc = frappe.get_doc("MS Community Post", post)
	if not _may_see(persona, doc):
		frappe.throw(_("This post is not for you."), frappe.PermissionError)

	existing = frappe.db.get_value(
		"MS Post Like", {"post": post, "user": frappe.session.user}, "name"
	)
	if existing:
		frappe.delete_doc("MS Post Like", existing, ignore_permissions=True, force=True)
		liked = False
	else:
		frappe.get_doc(
			{
				"doctype": "MS Post Like",
				"post": post,
				"user": frappe.session.user,
				"liked_on": now(),
			}
		).insert(ignore_permissions=True)
		liked = True

	# Counted rather than incremented: two people liking at once would each
	# read the same old number and write the same new one.
	count = frappe.db.count("MS Post Like", {"post": post})
	frappe.db.set_value("MS Community Post", post, "like_count", count, update_modified=False)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"liked": liked, "like_count": count},
		"message_en": "Liked." if liked else "Like removed.",
		"message_ar": "تم الإعجاب." if liked else "تم إلغاء الإعجاب.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def add_comment(
	post: str = None, body: str = None, parent_comment: str = None, persona: str = None
):
	"""Comment on a post, or reply to a comment."""
	body = (body or "").strip()
	if not post or not body:
		return fail(
			message_en="A post and a comment are required.",
			message_ar="يجب تحديد المنشور ونص التعليق.",
		)
	if len(body) > MAX_COMMENT_CHARS:
		return fail(
			message_en=f"A comment is at most {MAX_COMMENT_CHARS} characters.",
			message_ar=f"الحد الأقصى للتعليق {MAX_COMMENT_CHARS} حرف.",
		)

	doc = frappe.get_doc("MS Community Post", post)
	if not _may_see(persona, doc):
		frappe.throw(_("This post is not for you."), frappe.PermissionError)
	if not cint(doc.allow_comments):
		frappe.throw(_("Comments are closed on this post."), frappe.PermissionError)

	# A reply has to belong to the same post, or it would appear under a
	# conversation it was never part of.
	if parent_comment:
		owner_post = frappe.db.get_value("MS Post Comment", parent_comment, "post")
		if owner_post != post:
			return fail(
				message_en="That comment is on a different post.",
				message_ar="هذا التعليق يخص منشوراً آخر.",
			)

	comment = frappe.get_doc(
		{
			"doctype": "MS Post Comment",
			"post": post,
			"parent_comment": parent_comment or None,
			"author": frappe.session.user,
			"author_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
			"posted_on": now(),
			"body": body,
		}
	).insert(ignore_permissions=True)

	count = frappe.db.count("MS Post Comment", {"post": post, "is_hidden": 0})
	frappe.db.set_value("MS Community Post", post, "comment_count", count, update_modified=False)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": comment.name, "comment_count": count},
		"message_en": "Comment added.",
		"message_ar": "تمت إضافة التعليق.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def delete_comment(comment: str = None, persona: str = None):
	"""Remove your own comment. Staff may remove any."""
	if not comment:
		return fail(message_en="A comment is required.", message_ar="يجب تحديد التعليق.")
	doc = frappe.get_doc("MS Post Comment", comment)
	if persona not in STAFF and doc.author != frappe.session.user:
		frappe.throw(_("This comment is not yours."), frappe.PermissionError)

	post = doc.post
	# Replies to a deleted comment would hang under nothing.
	for child in frappe.get_all("MS Post Comment", filters={"parent_comment": comment}, pluck="name"):
		frappe.delete_doc("MS Post Comment", child, ignore_permissions=True, force=True)
	frappe.delete_doc("MS Post Comment", comment, ignore_permissions=True, force=True)

	count = frappe.db.count("MS Post Comment", {"post": post, "is_hidden": 0})
	frappe.db.set_value("MS Community Post", post, "comment_count", count, update_modified=False)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"deleted": comment, "comment_count": count},
		"message_en": "Comment removed.",
		"message_ar": "تم حذف التعليق.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def hide_comment(comment: str = None, reason: str = None, hidden: int = 1, persona: str = None):
	"""Hide a comment from families without destroying it.

	Moderation that deletes leaves no record of what was said or why it was
	removed. Hiding keeps both, which is what a school needs if a parent asks.
	"""
	if not comment:
		return fail(message_en="A comment is required.", message_ar="يجب تحديد التعليق.")
	doc = frappe.get_doc("MS Post Comment", comment)
	doc.is_hidden = cint(hidden)
	doc.hidden_reason = reason if cint(hidden) else None
	doc.save(ignore_permissions=True)

	count = frappe.db.count("MS Post Comment", {"post": doc.post, "is_hidden": 0})
	frappe.db.set_value("MS Community Post", doc.post, "comment_count", count, update_modified=False)
	frappe.db.commit()
	return {
		"success": True,
		"data": {"id": comment, "hidden": bool(cint(hidden))},
		"message_en": "Comment hidden." if cint(hidden) else "Comment restored.",
		"message_ar": "تم إخفاء التعليق." if cint(hidden) else "تم إظهار التعليق.",
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def upload_photo(persona: str = None):
	"""Store one image for a post."""
	uploaded = (frappe.request.files or {}).get("file")
	if not uploaded:
		return fail(message_en="No file received.", message_ar="لم يتم استلام أي ملف.")

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
			# Private: these are photographs of identifiable children.
			"is_private": 1,
			"attached_to_doctype": "MS Community Post",
			"attached_to_name": frappe.form_dict.get("post") or None,
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


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def channels(persona: str = None):
	"""The subjects this caller's feed divides into, with how many posts each holds.

	A pupil takes eight subjects; a parent of three children may see twenty.
	One wall means the maths post scrolls past while they are looking for it,
	so the feed is offered as channels — all, the general school one, and a
	channel per subject they actually study or teach.

	Counted, because a channel with nothing in it is a tab that wastes a tap.
	"""
	scope = resolve_scope(persona)
	groups = _my_groups(persona)

	# Which subjects belong to this caller. A family's subjects are their
	# children's; a teacher's are what they teach; the office sees everything.
	if persona in BACK_OFFICE:
		course_names = None
	else:
		rows = frappe.get_all(
			"MS Timetable Slot",
			filters={"student_group": ["in", groups or [""]], "active": 1},
			fields=["course"],
			limit_page_length=0,
		)
		course_names = sorted({r.course for r in rows if r.course})

	visible: dict = {}
	if persona not in BACK_OFFICE:
		visible["is_published"] = 1

	# One query for one column. Frappe refuses a COUNT in the field list when
	# or_filters are in play, and the visibility rules need those — so the
	# course column is plucked and tallied here rather than by the database.
	# One column across a school's posts is cheap; the alternative was a query
	# per subject.
	counts: dict[str, int] = {}
	general = 0
	for value in frappe.get_all(
		"MS Community Post",
		filters=visible,
		or_filters=_visible_filter(persona),
		pluck="course",
		limit_page_length=0,
	):
		if value:
			counts[value] = counts.get(value, 0) + 1
		else:
			general += 1

	if course_names is None:
		course_names = sorted(counts)

	labels = {
		r.name: r.course_name
		for r in frappe.get_all(
			"Course",
			filters={"name": ["in", course_names or [""]]},
			fields=["name", "course_name"],
		)
	}

	out = [
		{
			"key": "",
			"label": "الكل",
			"count": sum(counts.values()) + general,
			"kind": "all",
		},
		{
			"key": GENERAL_CHANNEL,
			"label": "عام المدرسة",
			"count": general,
			"kind": "general",
		},
	]
	for name in course_names:
		out.append(
			{
				"key": name,
				"label": labels.get(name) or name,
				"count": counts.get(name, 0),
				"kind": "course",
			}
		)

	return {"channels": out}
