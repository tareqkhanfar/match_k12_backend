"""مساحات العمل — named sets of buttons a person works from.

Until now everyone saw their role's menu (`src/lib/nav.ts`), give or take what
`workspace.save_layout` hid. A school's departments do not follow roles,
though: the counselling team wants its twelve forms and the student file on
one screen, not forty-seven entries to look past. A workspace is that screen,
built from the web without code —

- **blocks**: big buttons, lists of links, counted cards, a note;
- **buttons** open one of the system's screens, a built page
  (`custom_pages`), or an outside address.

People get workspaces two ways: by name (`MS Workspace Assignment`, which
also says which one they land on and whether their role's menu stays), or by
role (`personas` on the workspace — every teacher, say). With neither, nothing
changes: the role's menu is their workspace, as before.

Only the Administrator account builds and hands them out (the user's rule).
Screens a button points at keep their own permissions — a workspace arranges,
it does not grant.
"""

import json
import re

import frappe
from frappe.utils import cint

from match_schools.api import custom_pages, forms
from match_schools.api.custom_pages import assert_administrator, is_administrator
from match_schools.api.utils import (
	PERSONA_LABELS_AR,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_persona,
	ms_endpoint,
	parse_json_arg,
)

ALL_ROLES = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
BLOCK_TYPES = ("shortcuts", "links", "numbers", "text")
ITEM_KINDS = ("screen", "page", "url")

CATEGORY_ICONS = {
	"nursing": "stethoscope",
	"counselling": "heart-handshake",
	"special_needs": "accessibility",
	"learning_difficulties": "book-open-check",
	"speech_language": "speech",
	"school": "school",
}


def _json(value, default):
	return custom_pages._json(value, default)


def _short(value, size: int) -> str:
	return str(value or "").strip()[:size]


# --- layout ---------------------------------------------------------------------


def normalize_layout(data) -> dict:
	"""Keep blocks and buttons that make sense; drop the rest quietly."""
	data = data if isinstance(data, dict) else {}
	blocks = []
	for b in (data.get("blocks") or [])[:40]:
		if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
			continue
		block = {
			"id": _short(b.get("id"), 24) or frappe.generate_hash(length=8),
			"type": b["type"],
			"title": _short(b.get("title"), 120),
		}
		if b["type"] == "text":
			block["text"] = _short(b.get("text"), 3000)
		elif b["type"] == "numbers":
			block["cards"] = [_short(c, 60) for c in (b.get("cards") or []) if c][:24]
		else:
			items = []
			for it in (b.get("items") or [])[:80]:
				if not isinstance(it, dict) or it.get("kind") not in ITEM_KINDS:
					continue
				target = _short(it.get("target"), 500)
				kind = it["kind"]
				if kind == "screen" and not target.startswith("/app"):
					continue
				if kind == "url" and not re.match(r"^https?://", target):
					continue
				if kind == "page" and not frappe.db.exists("MS Custom Page", target):
					continue
				items.append(
					{
						"id": _short(it.get("id"), 24) or frappe.generate_hash(length=8),
						"kind": kind,
						"target": target,
						"label": _short(it.get("label"), 80),
						"icon": _short(it.get("icon"), 40),
						"color": _short(it.get("color"), 20),
					}
				)
			block["items"] = items
		blocks.append(block)
	return {"blocks": blocks}


def _resolve(layout: dict, persona: str) -> dict:
	"""The layout as this person may use it: pages they may not open are left
	out, and a page button with no label of its own takes the page's."""
	page_ids = {
		it["target"]
		for b in layout.get("blocks", [])
		for it in b.get("items", [])
		if it["kind"] == "page"
	}
	pages = {
		p.name: p
		for p in frappe.get_all(
			"MS Custom Page",
			filters={"name": ["in", sorted(page_ids) or [""]]},
			fields=["name", "title", "icon", "color", "is_active", "personas"],
		)
	}
	blocks = []
	for b in layout.get("blocks", []):
		block = dict(b)
		if "items" in b:
			items = []
			for it in b["items"]:
				if it["kind"] == "page":
					page = pages.get(it["target"])
					if not page or not custom_pages.may_open(page, persona):
						continue
					it = {
						**it,
						"label": it.get("label") or page.title,
						"icon": it.get("icon") or page.icon or "",
						"color": it.get("color") or page.color or "",
					}
				items.append(it)
			block["items"] = items
		blocks.append(block)
	return {"blocks": blocks}


def _row(doc, users: int | None = None) -> dict:
	layout = normalize_layout(_json(doc.get("layout"), {}))
	return {
		"id": doc.name,
		"title": doc.title,
		"icon": doc.icon or "",
		"color": doc.color or "",
		"description": doc.description or "",
		"isActive": cint(doc.is_active),
		"sortOrder": cint(doc.sort_order),
		"personas": [p for p in _json(doc.get("personas"), []) if p in ALL_ROLES],
		"buttons": sum(len(b.get("items", [])) for b in layout["blocks"]),
		**({"users": users} if users is not None else {}),
	}


# --- assignments -------------------------------------------------------------------


def _assignment(user: str) -> dict:
	row = frappe.db.get_value(
		"MS Workspace Assignment",
		{"user": user},
		["workspaces", "default_workspace", "hide_system"],
		as_dict=True,
	)
	if not row:
		return {"workspaces": [], "default": "", "hideSystem": 0}
	return {
		"workspaces": [w for w in _json(row.workspaces, []) if isinstance(w, str)],
		"default": row.default_workspace or "",
		"hideSystem": cint(row.hide_system),
	}


def _all_assignments() -> dict[str, dict]:
	out = {}
	for row in frappe.get_all(
		"MS Workspace Assignment",
		fields=["user", "workspaces", "default_workspace", "hide_system"],
		limit_page_length=0,
	):
		out[row.user] = {
			"workspaces": [w for w in _json(row.workspaces, []) if isinstance(w, str)],
			"default": row.default_workspace or "",
			"hideSystem": cint(row.hide_system),
		}
	return out


def _role_workspaces(persona: str) -> list[str]:
	"""Active workspaces every user of this role has."""
	rows = frappe.get_all(
		"MS Workspace",
		filters={"is_active": 1},
		fields=["name", "personas"],
		order_by="sort_order asc, title asc",
		limit_page_length=0,
	)
	return [r.name for r in rows if persona in _json(r.personas, [])]


def _write_assignment(user: str, workspaces: list[str], default: str, hide_system: int):
	existing = set(frappe.get_all("MS Workspace", pluck="name", limit_page_length=0))
	clean = []
	for w in workspaces:
		if w in existing and w not in clean:
			clean.append(w)
	persona = get_persona(user)
	reachable = set(clean) | set(_role_workspaces(persona) if persona else [])
	default = default if default in reachable else ""
	hide_system = 1 if cint(hide_system) and reachable else 0
	name = frappe.db.get_value("MS Workspace Assignment", {"user": user}, "name")
	if not clean and not default and not hide_system:
		if name:
			frappe.delete_doc("MS Workspace Assignment", name, ignore_permissions=True)
		return
	doc = frappe.get_doc("MS Workspace Assignment", name) if name else frappe.new_doc("MS Workspace Assignment")
	doc.user = user
	doc.workspaces = json.dumps(clean)
	doc.default_workspace = default
	doc.hide_system = hide_system
	doc.save(ignore_permissions=True)


# --- what a person sees --------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def mine(persona: str = None):
	"""The workspaces this person may switch between, and where they land.

	`default` empty means their role's menu, as before workspaces existed."""
	mine_ = _assignment(frappe.session.user)
	ids = list(mine_["workspaces"])
	for w in _role_workspaces(persona):
		if w not in ids:
			ids.append(w)
	docs = {
		d.name: d
		for d in frappe.get_all(
			"MS Workspace",
			filters={"name": ["in", ids or [""]], "is_active": 1},
			fields=["name", "title", "icon", "color", "description", "layout", "personas", "is_active", "sort_order"],
		)
	}
	out = []
	for w in ids:
		d = docs.get(w)
		if not d:
			continue
		out.append(
			{
				"id": d.name,
				"title": d.title,
				"icon": d.icon or "",
				"color": d.color or "",
				"description": d.description or "",
				"layout": _resolve(normalize_layout(_json(d.layout, {})), persona),
			}
		)
	available = {w["id"] for w in out}
	default = mine_["default"] if mine_["default"] in available else ""
	hide = bool(mine_["hideSystem"]) and bool(out)
	if hide and not default:
		default = out[0]["id"]
	return {
		"workspaces": out,
		"default": default,
		"hideSystem": hide,
		"canBuild": is_administrator(),
	}


# --- building (Administrator) -----------------------------------------------------------


def _users_of(workspace: str, assignments: dict | None = None) -> list[dict]:
	assignments = assignments if assignments is not None else _all_assignments()
	users = [u for u, a in assignments.items() if workspace in a["workspaces"]]
	names = dict(
		frappe.get_all("User", filters={"name": ["in", users or [""]]}, fields=["name", "full_name"], as_list=True)
	)
	out = []
	for u in users:
		p = get_persona(u)
		out.append(
			{
				"user": u,
				"name": names.get(u) or u,
				"persona": p or "",
				"personaLabel": PERSONA_LABELS_AR.get(p, p or ""),
				"isDefault": assignments[u]["default"] == workspace,
			}
		)
	return sorted(out, key=lambda r: r["name"])


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def list_workspaces(persona: str = None):
	assert_administrator()
	assignments = _all_assignments()
	counts = {}
	for a in assignments.values():
		for w in a["workspaces"]:
			counts[w] = counts.get(w, 0) + 1
	docs = frappe.get_all(
		"MS Workspace",
		fields=["name", "title", "icon", "color", "description", "is_active", "sort_order", "personas", "layout"],
		order_by="sort_order asc, title asc",
		limit_page_length=0,
	)
	return {"workspaces": [_row(d, counts.get(d.name, 0)) for d in docs]}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def get_workspace(workspace: str, persona: str = None):
	assert_administrator()
	doc = frappe.get_doc("MS Workspace", workspace)
	return {
		**_row(doc),
		"layout": normalize_layout(_json(doc.layout, {})),
		"assigned": _users_of(doc.name),
	}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def catalog(persona: str = None):
	"""What a button can point at besides the system's screens (which the
	web app lists itself): the built pages, and the counted cards."""
	assert_administrator()
	from match_schools.api.workspace import _cards_for

	cards = {}
	for p in (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT):
		for c in _cards_for(p):
			entry = cards.setdefault(c["key"], {"key": c["key"], "label": c["label"], "personas": []})
			entry["personas"].append(p)
	pages = frappe.get_all(
		"MS Custom Page",
		fields=["name", "title", "icon", "color", "description", "is_active", "template", "personas"],
		order_by="title asc",
		limit_page_length=0,
	)
	return {
		"pages": [custom_pages._page_row(p) for p in pages],
		"cards": list(cards.values()),
		"personas": [{"key": p, "label": PERSONA_LABELS_AR.get(p, p)} for p in ALL_ROLES],
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_workspace(payload: str | dict, persona: str = None):
	assert_administrator()
	data = parse_json_arg(payload) or {}
	title = _short(data.get("title"), 140)
	if not title:
		return fail("Give the workspace a name.", "اكتب اسم مساحة العمل.")
	doc = (
		frappe.get_doc("MS Workspace", data["id"])
		if data.get("id") and frappe.db.exists("MS Workspace", data["id"])
		else frappe.new_doc("MS Workspace")
	)
	doc.title = title
	doc.icon = _short(data.get("icon"), 40)
	doc.color = _short(data.get("color"), 20)
	doc.description = _short(data.get("description"), 500)
	doc.is_active = 1 if cint(data.get("isActive", 1)) else 0
	doc.sort_order = cint(data.get("sortOrder"))
	doc.personas = json.dumps([p for p in (data.get("personas") or []) if p in ALL_ROLES])
	doc.layout = json.dumps(normalize_layout(data.get("layout")), ensure_ascii=False)
	doc.save(ignore_permissions=True)

	if isinstance(data.get("assigned"), list):
		_sync_users(doc.name, data["assigned"])
	frappe.db.commit()
	return {
		**_row(doc),
		"layout": normalize_layout(_json(doc.layout, {})),
		"assigned": _users_of(doc.name),
	}


def _sync_users(workspace: str, wanted: list):
	"""Make exactly these people have this workspace, defaults as marked."""
	target = {}
	for row in wanted:
		user = row.get("user") if isinstance(row, dict) else row
		if user and get_persona(user):
			target[user] = bool(row.get("isDefault")) if isinstance(row, dict) else False
	assignments = _all_assignments()
	for user, a in assignments.items():
		if workspace in a["workspaces"] and user not in target:
			rest = [w for w in a["workspaces"] if w != workspace]
			default = "" if a["default"] == workspace else a["default"]
			_write_assignment(user, rest, default, a["hideSystem"])
	for user, is_default in target.items():
		a = assignments.get(user) or {"workspaces": [], "default": "", "hideSystem": 0}
		ws = a["workspaces"] + ([workspace] if workspace not in a["workspaces"] else [])
		default = workspace if is_default else ("" if a["default"] == workspace else a["default"])
		_write_assignment(user, ws, default, a["hideSystem"])


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def delete_workspace(workspace: str, persona: str = None):
	"""The workspace goes, and with it everyone's assignment to it. The pages
	its buttons opened stay."""
	assert_administrator()
	for user, a in _all_assignments().items():
		if workspace in a["workspaces"] or a["default"] == workspace:
			_write_assignment(
				user,
				[w for w in a["workspaces"] if w != workspace],
				"" if a["default"] == workspace else a["default"],
				a["hideSystem"],
			)
	frappe.delete_doc("MS Workspace", workspace, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": workspace}


@frappe.whitelist()
@ms_endpoint(ROLE_ADMIN)
def assignments(search: str = "", persona: str = None):
	"""People and the workspaces they have — the other way to hand them out."""
	assert_administrator()
	or_filters = (
		[["full_name", "like", f"%{search}%"], ["name", "like", f"%{search}%"]] if search else None
	)
	rows = frappe.get_all(
		"User",
		filters={"enabled": 1, "name": ["!=", "Guest"]},
		or_filters=or_filters,
		fields=["name", "full_name"],
		order_by="full_name",
		limit=120,
	)
	all_a = _all_assignments()
	out = []
	for r in rows:
		p = get_persona(r.name)
		if not p:
			continue
		a = all_a.get(r.name) or {"workspaces": [], "default": "", "hideSystem": 0}
		out.append(
			{
				"user": r.name,
				"name": r.full_name or r.name,
				"persona": p,
				"personaLabel": PERSONA_LABELS_AR.get(p, p),
				"workspaces": a["workspaces"],
				"roleWorkspaces": _role_workspaces(p),
				"default": a["default"],
				"hideSystem": a["hideSystem"],
			}
		)
	# People with something assigned first: they are the ones being managed.
	out.sort(key=lambda r: (0 if r["workspaces"] or r["default"] else 1, r["name"]))
	return {"users": out}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def save_assignment(
	user: str, workspaces: str | list = None, default: str = "", hide_system: int = 0, persona: str = None
):
	assert_administrator()
	if not get_persona(user):
		return fail("This user has no Match Schools role.", "هذا المستخدم بلا دور في النظام.")
	ws = parse_json_arg(workspaces, []) or []
	_write_assignment(user, [w for w in ws if isinstance(w, str)], default or "", cint(hide_system))
	frappe.db.commit()
	return {**_assignment(user), "user": user}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(ROLE_ADMIN)
def from_category(category: str, title: str = None, persona: str = None):
	"""One click from a forms file to a working department: a page for every
	active form in it (reusing a page a form already has), and a workspace
	with a button for each, plus the file itself and its reports."""
	assert_administrator()
	if category not in forms.CATEGORIES:
		return fail("Unknown file.", "قسم نماذج غير معروف.")
	templates = frappe.get_all(
		"MS Form Template",
		filters={"category": category, "is_active": 1},
		fields=["name", "title"],
		order_by="title asc",
		limit_page_length=0,
	)
	if not templates:
		return fail(
			"This file has no active forms.",
			f"لا توجد نماذج مفعّلة في قسم {forms.CATEGORIES[category]} — صمّم النماذج أولاً.",
		)
	created = 0
	page_ids = []
	for t in templates:
		existing = frappe.get_all("MS Custom Page", filters={"template": t.name}, pluck="name", limit=1)
		if existing:
			page_ids.append(existing[0])
			continue
		page_ids.append(custom_pages.create_page_for(frappe.get_doc("MS Form Template", t.name)))
		created += 1
	label = forms.CATEGORIES[category]
	doc = frappe.new_doc("MS Workspace")
	doc.title = _short(title, 140) or f"قسم {label}"
	doc.icon = CATEGORY_ICONS.get(category, "layout-grid")
	doc.color = "primary"
	doc.description = f"نماذج {label}: صفحة لكل نموذج للتعبئة والمتابعة والطباعة."
	doc.is_active = 1
	doc.personas = json.dumps([])
	doc.layout = json.dumps(
		normalize_layout(
			{
				"blocks": [
					{
						"type": "shortcuts",
						"title": f"نماذج {label}",
						"items": [{"kind": "page", "target": p} for p in page_ids],
					},
					{
						"type": "links",
						"title": "المتابعة",
						"items": [
							{"kind": "screen", "target": f"/app/forms/{category}", "label": f"ملف {label} — كل النماذج"},
							{"kind": "screen", "target": "/app/forms-reports", "label": "تقارير النماذج"},
							{"kind": "screen", "target": "/app/students", "label": "الطلاب"},
						],
					},
				]
			}
		),
		ensure_ascii=False,
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {
		"id": doc.name,
		"pagesCreated": created,
		"pages": len(page_ids),
	}
