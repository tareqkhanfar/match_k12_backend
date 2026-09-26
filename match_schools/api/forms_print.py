# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""How a specialist form prints.

A school's paper forms are cards with a coloured band, an icon and a title;
choices are printed as tick boxes with the chosen one ticked; an unanswered
line is a dotted line to write on; a letterhead carries the school, the
department, the form's title and a motto. This module draws exactly that, so
a school can design such a page from the form's fields alone — and a design
written by hand uses the same pieces:

    {{ letterhead() }}
    {{ box("موضوع الحصة", "Long Text") }}            a card: icon, label, answer
    {{ inline("عدد الطلبة", "Number") }}             label: answer on one line
    {{ checks("مدى التفاعل", "Select", "ممتاز|جيد") }}  the options as tick boxes
    {{ field("الوزن") }}                             the answer alone
    {{ card("عنوان", "users", "blue") }} … {{ endcard() }}
    {{ grid(2) }} … {{ endgrid() }}
    {{ info("الصف والشعبة", section_label) }}
    {{ signatures() }}  {{ footer() }}  {{ lines(3) }}  {{ icon("target") }}

`box`, `inline`, `checks` and `field` all name a field by its label, then its
type and options — which is what lets a design be read back into fields.
"""

import base64
import json
import mimetypes
import re

import frappe
from frappe.utils import cint, escape_html, get_fullname

# --- Vocabulary -----------------------------------------------------------------

TONES = {
	# background, border, ink (icon and header text)
	"violet": ("#f4f1fa", "#dcd5ef", "#6f5db5"),
	"green": ("#eff6ef", "#cfe3d0", "#3e8a58"),
	"blue": ("#edf3f9", "#cbdcec", "#3a73a4"),
	"orange": ("#fbf2e7", "#eed9bd", "#bb7428"),
	"pink": ("#fbeef0", "#efd0d6", "#bf5268"),
	"teal": ("#eaf5f3", "#c6e2de", "#2c8780"),
	"yellow": ("#fbf6e5", "#ece0b3", "#a3821b"),
	"gray": ("#f3f4f7", "#dcdfe7", "#586078"),
}
TONE_LABELS = {
	"violet": "بنفسجي",
	"green": "أخضر",
	"blue": "أزرق",
	"orange": "برتقالي",
	"pink": "وردي",
	"teal": "فيروزي",
	"yellow": "أصفر",
	"gray": "رمادي",
}
# The order cards take their colours in when the design does not choose.
TONE_CYCLE = ["violet", "green", "blue", "orange", "pink", "teal", "yellow"]

_ICON_PATHS = {
	"target": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
	"flag": '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
	"presentation": '<path d="M2 3h20"/><path d="M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3"/><path d="m7 21 5-5 5 5"/>',
	"gear": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1"/>',
	"users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
	"user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
	"note": '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
	"bulb": '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14"/>',
	"clipboard": '<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/>',
	"calendar": '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
	"book": '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
	"clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
	"heart": '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
	"star": '<path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>',
	"check": '<path d="M20 6 9 17l-5-5"/>',
	"info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
	"pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
	"list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
	"activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
	"message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
	"school": '<path d="M22 10 12 5 2 10l10 5 10-5z"/><path d="M6 12v5c3 3 9 3 12 0v-5"/>',
	"health": '<path d="M11 2a2 2 0 0 0-2 2v5H4a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h5v5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-5h5a2 2 0 0 0 2-2v-2a2 2 0 0 0-2-2h-5V4a2 2 0 0 0-2-2z"/>',
	"home": '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>',
	"phone": '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/>',
}
ICON_LABELS = {
	"target": "هدف",
	"flag": "راية",
	"presentation": "عرض / حصة",
	"gear": "أساليب / إعدادات",
	"users": "مجموعة",
	"user": "شخص",
	"note": "ملاحظة / قلم",
	"bulb": "فكرة",
	"clipboard": "قائمة مهام",
	"calendar": "تقويم",
	"book": "كتاب",
	"clock": "ساعة",
	"heart": "قلب",
	"star": "نجمة",
	"check": "صح",
	"info": "معلومة",
	"pin": "مكان",
	"list": "قائمة",
	"activity": "نشاط / نبض",
	"message": "رسالة",
	"school": "تعليم",
	"health": "صحة",
	"home": "أسرة / منزل",
	"phone": "هاتف",
}
# The icon a card takes when its field does not choose one.
_DEFAULT_ICON = {
	"Long Text": "note",
	"Select": "list",
	"Multi Select": "gear",
	"Checkbox": "check",
	"Table": "clipboard",
	"Student Table": "users",
	"Text Block": "info",
	"Rating": "star",
	"Date": "calendar",
	"Datetime": "calendar",
	"Time": "clock",
}
# Fields short enough to sit two or three to a row as «label: answer».
SHORT_TYPES = ("Data", "Number", "Date", "Time", "Datetime", "Rating", "Attach")
LAYOUT = ("Section", "Heading")

ENTRY_FOR = {"Student": "طالب", "Section": "شعبة", "General": "عام"}


def icon_svg(name: str, size: int = 18) -> str:
	path = _ICON_PATHS.get((name or "").strip())
	if not path:
		return ""
	return (
		f'<span class="ic"><svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
		'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
		f'stroke-linecap="round" stroke-linejoin="round">{path}</svg></span>'
	)


_LEAF = (
	'<svg class="deco {pos}" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">'
	'<path d="M4 60 C 16 46, 28 30, 52 6" fill="none" stroke="#86a98d" stroke-width="1.4" stroke-linecap="round"/>'
	'<g fill="#a8c6ac">'
	'<ellipse cx="17" cy="45" rx="7.5" ry="3.1" transform="rotate(-70 17 45)"/>'
	'<ellipse cx="22" cy="48" rx="7.5" ry="3.1" transform="rotate(5 22 48)"/>'
	'<ellipse cx="28" cy="31" rx="7" ry="3" transform="rotate(-75 28 31)"/>'
	'<ellipse cx="34" cy="35" rx="7" ry="3" transform="rotate(0 34 35)"/>'
	'<ellipse cx="41" cy="18" rx="6.5" ry="2.8" transform="rotate(-80 41 18)"/>'
	'<ellipse cx="46" cy="22" rx="6.5" ry="2.8" transform="rotate(-5 46 22)"/>'
	"</g></svg>"
)


def _tone_css() -> str:
	out = []
	for name, (bg, border, ink) in TONES.items():
		out.append(
			f".tone-{name}{{background:{bg};border-color:{border}}}"
			f".tone-{name} .ms-card-head,.tone-{name}>.ic,.ic.tone-{name}{{color:{ink}}}"
			f".ms-table th.tone-{name}{{background:{border};color:#262b3d}}"
		)
	return "".join(out)


PRINT_STYLE = (
	'<link rel="preconnect" href="https://fonts.googleapis.com">'
	'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
	'<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap" rel="stylesheet">'
	"""<style>
  @page { size: A4; margin: 8mm; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Tajawal", "Segoe UI", Tahoma, sans-serif; direction: rtl; color: #1f2433;
         -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  /* The earlier, plain look — kept for designs written against it. */
  .ms-form h1 { font-size: 20px; text-align: center; margin: 0 0 12px; }
  .ms-form table.head { width: 100%; border-collapse: collapse; margin-bottom: 10px; font-size: 12px; }
  .ms-form table.head td { border: 1px solid #ccd; padding: 5px 7px; }
  .ms-form .row { display: flex; gap: 6px; padding: 4px 2px; border-bottom: 1px dotted #ccd; font-size: 12px; }
  .ms-form .label { font-weight: 700; min-width: 160px; }
  .ms-form table.grid { width: 100%; border-collapse: collapse; font-size: 12px; margin: 6px 0; }
  .ms-form table.grid th, .ms-form table.grid td { border: 1px solid #ccd; padding: 4px 6px; }
  .theme-classic .ms-form h2 { font-size: 14px; background: #eef2f9; padding: 6px 8px; margin: 14px 0 6px; border-radius: 4px; }

  /* The page */
  .ms-page { position: relative; font-size: 12.5px; }
  .theme-soft { border: 1.5px solid #c9cde0; border-radius: 16px; padding: 12px 18px 10px; min-height: 276mm; }
  .theme-soft.landscape { min-height: 189mm; }
  /* An official letter: the school's own letterhead across the top, inside
     the double rule its paper documents carry. */
  .theme-letter { border: 3.5px double #1f2433; padding: 7mm 9mm 8mm; min-height: 279mm; font-size: 15px; line-height: 1.9; }
  .theme-letter.landscape { min-height: 192mm; }
  .ms-banner { margin: 0 0 4mm; }
  .ms-banner img { display: block; width: 100%; max-height: 42mm; object-fit: contain; }
  .deco { position: absolute; width: 54px; height: 54px; }
  .deco.tr { top: 3px; right: 3px; transform: scaleX(-1); }
  .deco.tl { top: 3px; left: 3px; }
  .deco.br { bottom: 3px; right: 3px; transform: scale(-1, -1); }
  .deco.bl { bottom: 3px; left: 3px; transform: scaleY(-1); }

  /* Letterhead */
  .ms-lh { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 36px 8px; }
  .lh-side { width: 19%; text-align: center; font-size: 11.5px; color: #55607a; line-height: 1.75; white-space: pre-line; }
  .lh-logo img { max-width: 100%; max-height: 92px; object-fit: contain; }
  .lh-center { flex: 1; text-align: center; }
  .lh-school { font-size: 17px; font-weight: 800; color: #2b3150; }
  .lh-dept { font-size: 13px; color: #4b5470; margin-top: 2px; display: flex; align-items: center; justify-content: center; gap: 8px; }
  .lh-dept::before, .lh-dept::after { content: ""; width: 54px; border-top: 1px solid #b9bfd6; }
  .lh-title { display: inline-block; white-space: nowrap; margin-top: 7px; padding: 6px 30px; border-radius: 999px; background: #ebe7f6; color: #2d2a4a; font-size: 19px; font-weight: 800; }
  .lh-plain { text-align: center; margin: 4px 36px 10px; }

  /* Cards */
  .ms-card { border: 1px solid; border-radius: 12px; margin: 6px 0; padding: 6px 12px 7px; break-inside: avoid; }
  .ms-card-head { display: flex; align-items: center; gap: 7px; font-weight: 800; font-size: 13.5px; margin-bottom: 3px; }
  .ms-card-head .t { color: #262b3d; }
  .ic { display: inline-flex; flex: none; }
  .ans { white-space: pre-wrap; font-weight: 500; padding: 1px 4px 3px; line-height: 1.85; }
  .ln { height: 21px; border-bottom: 1.2px dotted #8d93a8; margin: 0 4px; }
  .dots { display: inline-block; flex: 1; min-width: 60px; border-bottom: 1.2px dotted #8d93a8; height: 1.05em; margin-inline-start: 4px; }
  .inl { display: flex; align-items: baseline; gap: 4px; padding: 3px 2px; min-width: 0; }
  .inl > b { font-weight: 700; white-space: nowrap; }
  .inl .val { flex: 1; font-weight: 500; border-bottom: 1.2px dotted #8d93a8; padding: 0 6px; }
  .ms-grid { display: grid; gap: 2px 26px; align-items: start; }
  .ms-grid.cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ms-grid.cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .ms-grid.cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 2px 10px; }
  .ms-grid.cols-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 2px 8px; }
  .ms-grid > .ms-card { margin: 4px 0; height: calc(100% - 8px); }
  .opts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 5px 14px; padding: 3px 6px; }
  .ms-grid .opts, .opts.col { grid-template-columns: 1fr; gap: 4px; }
  .opt { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .ck { display: inline-block; width: 12px; height: 12px; border: 1.4px solid #4a5068; border-radius: 3px; position: relative; flex: none; background: #fff; }
  .ck.on::after { content: ""; position: absolute; left: 3px; top: 0; width: 3.5px; height: 7px; border: solid #1d2a55; border-width: 0 2px 2px 0; transform: rotate(45deg); }
  h2.section { font-size: 14.5px; font-weight: 800; margin: 10px 2px 3px; display: flex; align-items: center; gap: 7px; color: #2b3150; }
  h3.heading { font-size: 13px; font-weight: 700; margin: 8px 2px 2px; color: #3b4260; }
  .txt { line-height: 1.85; font-size: 11.5px; padding: 0 4px; white-space: pre-line; }

  /* Tables */
  .ms-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 11.5px; border: 1px solid #cfd3e2; border-radius: 10px; overflow: hidden; background: #fff; }
  .ms-table th { background: #eef0f7; font-weight: 700; padding: 5px 4px; border-inline-start: 1px solid #cfd3e2; border-bottom: 1px solid #cfd3e2; }
  .ms-table td { padding: 3px 5px; height: 24px; border-top: 1px dotted #b8bdd0; border-inline-start: 1px solid #e3e5ef; vertical-align: middle; }
  .ms-table tr > :first-child { border-inline-start: none; }
  .ms-table tbody tr:first-child td { border-top: none; }
  .ms-table.st { font-size: 10.5px; }
  .ms-table.st th.sub { font-size: 9px; font-weight: 600; line-height: 1.25; padding: 3px 2px; }
  .ms-table.st td.c { text-align: center; padding: 2px; }
  .ms-table.st td.c .ck { margin: auto; }
  .ms-table .n { width: 22px; text-align: center; color: #666; }
  .ms-table .name { text-align: right; white-space: nowrap; }

  /* Signatures and footer */
  .ms-sign { display: flex; justify-content: space-between; gap: 40px; margin: 14px 8px 2px; }
  .ms-sign .inl { flex: 1; }
  .ms-foot { display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 11px; color: #5a6280; margin-top: 8px; }
  .ms-foot::before, .ms-foot::after { content: ""; width: 56px; border-top: 1px solid #aab0c4; }
  @media print { .no-print { display: none; } }
"""
	+ _tone_css()
	+ "</style>"
)


def css_block(css: str | None) -> str:
	"""A form's own CSS, after the base style so it can override it. A stray
	`</style>` would end the block and let the rest in as markup."""
	css = (css or "").strip()
	if not css:
		return ""
	return "<style>\n" + css.replace("</style", "") + "\n</style>"


def options_of(f: dict) -> list[str]:
	return [o.strip() for o in (f.get("options") or "").split("\n") if o.strip()]


def _student_columns(f: dict) -> list[dict]:
	"""`الوضع السلوكي: ممتاز|جيد` is a column of tick boxes; a bare title is a
	column to write in."""
	cols = []
	for line in options_of(f):
		if ":" in line:
			title, opts = line.split(":", 1)
			choices = [o.strip() for o in opts.split("|") if o.strip()]
			if choices:
				cols.append({"title": title.strip(), "choices": choices})
				continue
		cols.append({"title": line.rstrip(":").strip(), "choices": []})
	return cols


def _json_rows(raw) -> list[dict]:
	if not raw:
		return []
	try:
		rows = json.loads(raw)
		return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
	except Exception:
		return []


def _fmt_date(raw: str) -> str:
	m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw or ""))
	# Isolated left-to-right, or an Arabic line shows it year-first.
	return f"\u2066{m.group(3)} / {m.group(2)} / {m.group(1)}\u2069" if m else str(raw or "")


def file_data_uri(url: str) -> str:
	"""An image inlined, so the printed page needs nothing from the server."""
	if not url:
		return ""
	try:
		name = frappe.db.get_value("File", {"file_url": url}, "name")
		if name:
			content = frappe.get_doc("File", name).get_content()
			if isinstance(content, str):
				content = content.encode()
			mime = mimetypes.guess_type(url)[0] or "image/png"
			return f"data:{mime};base64," + base64.b64encode(content).decode()
	except Exception:
		pass
	return frappe.utils.get_url(url)


class Page:
	"""One render: the fields, the answers and the helpers a design calls."""

	def __init__(self, template_doc, entry_doc, fields: list[dict], blank: bool = False):
		self.t = template_doc
		self.e = entry_doc
		self.fields = fields
		self.blank = blank
		self.raw = {} if blank else {v.fieldname: v.value for v in (entry_doc.values_table or [])}
		self.by_label = {f["label"]: f for f in fields}
		self.by_name = {f["fieldname"]: f for f in fields}
		self._tone = 0

	# -- lookups ---------------------------------------------------------------

	def _find(self, label):
		label = str(label or "")
		return self.by_label.get(label.rstrip(" *").strip()) or self.by_name.get(label)

	def _next_tone(self) -> str:
		tone = TONE_CYCLE[self._tone % len(TONE_CYCLE)]
		self._tone += 1
		return tone

	# -- answers ---------------------------------------------------------------

	def _chosen(self, f) -> set[str]:
		raw = self.raw.get(f["fieldname"]) or ""
		if f["fieldtype"] == "Multi Select":
			return {x.strip() for x in re.split(r"،|,|\n", raw) if x.strip()}
		return {raw.strip()} if raw.strip() else set()

	def _text(self, f) -> str:
		"""The answer as escaped text, or '' when there is none."""
		raw = self.raw.get(f["fieldname"])
		ft = f["fieldtype"]
		if ft == "Text Block":
			return escape_html(f.get("options") or "")
		if ft in ("Table", "Student Table"):
			# Drawn even when empty: blank rows are the lines to write on.
			return self._table(f)
		if raw in (None, ""):
			return ""
		if ft == "Checkbox":
			return "نعم" if cint(raw) else "لا"
		if ft == "Rating":
			return f"{cint(raw)} / 5"
		if ft == "Date":
			return _fmt_date(raw)
		if ft == "Datetime":
			return _fmt_date(raw) + " " + str(raw)[11:16]
		if ft == "Multi Select":
			chosen = self._chosen(f)
			ordered = [o for o in options_of(f) if o in chosen] + sorted(chosen - set(options_of(f)))
			return escape_html("، ".join(o.rstrip(":") for o in ordered))
		if ft == "Attach":
			return escape_html(str(raw).rsplit("/", 1)[-1])
		return escape_html(str(raw))

	def _ticks(self, f, column: bool = False) -> str:
		options = ["نعم", "لا"] if f["fieldtype"] == "Checkbox" else options_of(f)
		chosen = self._chosen(f)
		if f["fieldtype"] == "Checkbox" and not self.blank and self.raw.get(f["fieldname"]) not in (None, ""):
			chosen = {"نعم" if cint(self.raw.get(f["fieldname"])) else "لا"}
		items = []
		for o in options:
			# «أخرى:» leaves a line to write on.
			trail = o.endswith(":")
			label = o.rstrip(":").strip()
			on = " on" if (label in chosen or o in chosen) else ""
			items.append(
				f'<span class="opt"><span class="ck{on}"></span>{escape_html(label)}'
				+ (':<span class="dots"></span>' if trail else "")
				+ "</span>"
			)
		return f'<div class="opts{" col" if column else ""}">' + "".join(items) + "</div>"

	def _lines(self, f, default: int = 1) -> str:
		n = cint(f.get("print_rows")) or default
		return '<div class="ln"></div>' * max(1, min(n, 40))

	def _table(self, f) -> str:
		if f["fieldtype"] == "Student Table":
			return self._student_table(f)
		cols = options_of(f) or ["—"]
		rows = [] if self.blank else _json_rows(self.raw.get(f["fieldname"]))
		pad = cint(f.get("print_rows")) or (8 if not rows else 0)
		tones = ["orange", "blue", "green", "violet", "pink", "teal", "yellow"]
		head = "".join(
			f'<th class="tone-{tones[i % len(tones)]}">{escape_html(c)}</th>' for i, c in enumerate(cols)
		)
		body = []
		for r in rows:
			body.append("<tr>" + "".join(f"<td>{escape_html(str(r.get(c, '') or ''))}</td>" for c in cols) + "</tr>")
		for _ in range(max(0, pad - len(rows))):
			body.append("<tr>" + "<td></td>" * len(cols) + "</tr>")
		return f'<table class="ms-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'

	def _student_table(self, f) -> str:
		cols = _student_columns(f)
		rows = [] if self.blank else _json_rows(self.raw.get(f["fieldname"]))
		pad = cint(f.get("print_rows")) or (12 if not rows else 0)
		tones = ["violet", "green", "pink", "blue", "orange", "teal", "yellow"]
		top = ['<th class="n" rowspan="2">م</th>', '<th class="name" rowspan="2">اسم الطالب/ة</th>']
		sub = []
		ti = 0
		for c in cols:
			if c["choices"]:
				tone = tones[ti % len(tones)]
				ti += 1
				top.append(f'<th class="tone-{tone}" colspan="{len(c["choices"])}">{escape_html(c["title"])}</th>')
				sub += [f'<th class="sub tone-{tone}">{escape_html(o)}</th>' for o in c["choices"]]
			else:
				top.append(f'<th rowspan="2" style="min-width:90px">{escape_html(c["title"])}</th>')
		body = []
		for i, r in enumerate(rows, 1):
			cells = [f'<td class="n">{i}</td>', f'<td class="name">{escape_html(str(r.get("name") or ""))}</td>']
			for c in cols:
				v = str(r.get(c["title"]) or "")
				if c["choices"]:
					cells += [f'<td class="c"><span class="ck{" on" if v == o else ""}"></span></td>' for o in c["choices"]]
				else:
					cells.append(f"<td>{escape_html(v)}</td>")
			body.append("<tr>" + "".join(cells) + "</tr>")
		for j in range(len(rows) + 1, max(len(rows), pad) + 1):
			body.append(
				f'<tr><td class="n">{j}</td><td class="name"></td>'
				+ "".join(
					'<td class="c"><span class="ck"></span></td>' * len(c["choices"]) if c["choices"] else "<td></td>"
					for c in cols
				)
				+ "</tr>"
			)
		return (
			'<table class="ms-table st"><thead><tr>' + "".join(top) + "</tr><tr>" + "".join(sub)
			+ "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
		)

	# -- helpers a design calls ------------------------------------------------

	def field(self, label, *_a, **_k):
		f = self._find(label)
		if not f:
			return ""
		return self._text(f) or '<span class="dots"></span>'

	def inline(self, label, *_a, **_k):
		f = self._find(label)
		if not f:
			return ""
		text = self._text(f)
		value = f'<span class="val">{text}</span>' if text else '<span class="dots"></span>'
		return f'<div class="inl"><b>{escape_html(f["label"])}:</b>{value}</div>'

	def checks(self, label, *_a, **_k):
		f = self._find(label)
		if not f:
			return ""
		if f["fieldtype"] in ("Select", "Multi Select", "Checkbox"):
			return self._ticks(f)
		return self._text(f) or self._lines(f)

	def box(self, label, *_a, icon: str = "", tone: str = "", **_k):
		f = self._find(label)
		if not f:
			return ""
		ft = f["fieldtype"]
		head_icon = icon or f.get("icon") or _DEFAULT_ICON.get(ft, "")
		out = self.card(f["label"], head_icon, tone or f.get("tone") or "")
		if ft in ("Select", "Multi Select", "Checkbox"):
			out += self._ticks(f)
		elif ft in ("Table", "Student Table"):
			out += self._table(f)
		elif ft == "Text Block":
			out += f'<div class="txt">{self._text(f)}</div>'
		else:
			text = self._text(f)
			out += f'<div class="ans">{text}</div>' if text else self._lines(f, 2 if ft == "Long Text" else 1)
		return out + self.endcard()

	def card(self, title: str = "", icon: str = "", tone: str = ""):
		tone = tone if tone in TONES else self._next_tone()
		head = ""
		if title:
			head = f'<div class="ms-card-head">{icon_svg(icon)}<span class="t">{escape_html(str(title))}:</span></div>'
		return f'<section class="ms-card tone-{tone}">{head}<div class="ms-card-body">'

	def endcard(self):
		return "</div></section>"

	def grid(self, cols: int = 2):
		return f'<div class="ms-grid cols-{max(1, min(cint(cols) or 2, 5))}">'

	def endgrid(self):
		return "</div>"

	def info(self, label, value=""):
		value = "" if value is None else str(value)
		inner = f'<span class="val">{escape_html(value)}</span>' if value.strip() else '<span class="dots"></span>'
		return f'<div class="inl"><b>{escape_html(str(label))}:</b>{inner}</div>'

	def section(self, label, *_a, icon: str = "", **_k):
		f = self._find(label)
		if f and f["fieldtype"] == "Heading":
			return f'<h3 class="heading">{escape_html(str(label))}</h3>'
		return f'<h2 class="section">{icon_svg(icon or (f or {}).get("icon") or "")}{escape_html(str(label))}</h2>'

	def lines(self, n: int = 1):
		return '<div class="ln"></div>' * max(1, min(cint(n) or 1, 40))

	def icon(self, name: str, tone: str = ""):
		svg = icon_svg(name)
		return svg.replace('class="ic"', f'class="ic tone-{tone}"', 1) if tone in TONES else svg

	def letterhead(self, logo: bool = True, school: bool = True, motto: bool = True):
		t = self.t
		company = frappe.db.get_default("company") or ""
		name = t.get("print_school") or (
			frappe.db.get_value("Company", company, "company_name") if company else ""
		) or company
		logo_url = t.get("print_logo") or (
			frappe.db.get_value("Company", company, "company_logo") if company else ""
		)
		dept = t.get("print_department") or ""
		center = ""
		if school and name:
			center += f'<div class="lh-school">{escape_html(name)}</div>'
		if dept:
			center += f'<div class="lh-dept">{escape_html(dept)}</div>'
		center += f'<div class="lh-title">{escape_html(t.title or "")}</div>'
		left = f'<img src="{file_data_uri(logo_url)}" alt="">' if (logo and logo_url) else ""
		right = escape_html(t.get("print_motto") or "") if motto else ""
		if not left and not right:
			return f'<header class="lh-plain">{center}</header>'
		return (
			'<header class="ms-lh">'
			f'<div class="lh-side lh-motto">{right}</div>'
			f'<div class="lh-center">{center}</div>'
			f'<div class="lh-side lh-logo">{left}</div>'
			"</header>"
		)

	def banner(self):
		"""The letterhead as one image — the school's own, across the page.

		For letters that must look like the school's paper: the form's logo is
		the whole letterhead (Arabic side, crest, English side), printed at full
		width. Without one, the ordinary letterhead is drawn instead.
		"""
		url = self.t.get("print_logo")
		if not url:
			return self.letterhead()
		return f'<div class="ms-banner"><img src="{file_data_uri(url)}" alt=""></div>'

	def weekday(self, label):
		"""The Arabic weekday of a date answer («الأحد»), or '' when unanswered."""
		f = self._find(label)
		raw = self.raw.get(f["fieldname"]) if f else None
		m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw or ""))
		if not m:
			return ""
		import datetime

		days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
		return days[datetime.date(int(m[1]), int(m[2]), int(m[3])).weekday()]

	def chosen(self, label) -> list[str]:
		"""A multiple choice's picks, in the order the form lists them."""
		f = self._find(label)
		if not f:
			return []
		picked = self._chosen(f)
		return [o.rstrip(":") for o in options_of(f) if o in picked or o.rstrip(":") in picked] + sorted(
			x for x in picked if x not in options_of(f) and x + ":" not in options_of(f)
		)

	def _student(self) -> dict:
		"""What the student's record already says, for a design to fall back on."""
		sid = None if self.blank else self.e.get("student")
		if not sid or not frappe.db.exists("Student", sid):
			return {}
		st = frappe.db.get_value(
			"Student",
			sid,
			["student_name", "date_of_birth", "joining_date", "city", "address_line_1", "gender"],
			as_dict=True,
		) or {}
		guardian = frappe.get_all(
			"Student Guardian",
			filters={"parent": sid, "parenttype": "Student"},
			fields=["guardian_name"],
			order_by="idx",
			limit=1,
		)
		enrolment = frappe.get_all(
			"Program Enrollment",
			filters={"student": sid, "docstatus": 1},
			fields=["program", "academic_year"],
			order_by="enrollment_date desc",
			limit=1,
		)
		return {
			"name": st.get("student_name") or "",
			"date_of_birth": _fmt_date(str(st.get("date_of_birth") or "")),
			"joining_date": _fmt_date(str(st.get("joining_date") or "")),
			"city": st.get("city") or st.get("address_line_1") or "",
			"female": (st.get("gender") or "") in ("Female", "أنثى"),
			"guardian": guardian[0].guardian_name if guardian else "",
			"program": enrolment[0].program if enrolment else "",
			"academic_year": enrolment[0].academic_year if enrolment else "",
		}

	def signatures(self):
		labels = [x.strip() for x in (self.t.get("print_signatures") or "").split("\n") if x.strip()]
		if not labels:
			return ""
		return '<div class="ms-sign">' + "".join(self.info(x) for x in labels) + "</div>"

	def footer(self):
		text = (self.t.get("print_footer") or "").strip()
		return f'<footer class="ms-foot">{escape_html(text)}</footer>' if text else ""

	# -- the page ---------------------------------------------------------------

	def context(self) -> dict:
		e = self.e
		pretty = {}
		for f in self.fields:
			if f["fieldtype"] not in LAYOUT:
				pretty[f["fieldname"]] = self._text(f)
		group = e.get("student_group") if not self.blank else None
		section_label = (
			frappe.db.get_value("Student Group", group, "student_group_name") or group if group else ""
		)
		filled = e.get("filled_on") if not self.blank else None
		return {
			"entry": e,
			"template": self.t,
			"fields": self.fields,
			"values": pretty,
			"school": frappe.db.get_default("company") or "",
			"blank": self.blank,
			"student_name": "" if self.blank else (e.get("student_name") or ""),
			"section_label": section_label,
			"filled_on": str(filled)[:16] if filled else "",
			"filled_date": _fmt_date(str(filled)[:10]) if filled else "",
			"filled_by_name": "" if self.blank or not e.get("filled_by") else get_fullname(e.filled_by),
			"field": self.field,
			"inline": self.inline,
			"checks": self.checks,
			"box": self.box,
			"card": self.card,
			"endcard": self.endcard,
			"grid": self.grid,
			"endgrid": self.endgrid,
			"info": self.info,
			"section": self.section,
			"lines": self.lines,
			"icon": self.icon,
			"letterhead": self.letterhead,
			"banner": self.banner,
			"weekday": self.weekday,
			"chosen": self.chosen,
			"student": self._student(),
			"signatures": self.signatures,
			"footer": self.footer,
		}


def render(template_doc, entry_doc, fields: list[dict], blank: bool = False) -> str:
	"""The whole printable page: style, the form's CSS, the frame and the design."""
	theme = template_doc.get("print_theme") or "soft"
	landscape = (template_doc.get("print_orientation") or "") == "Landscape"
	design = template_doc.get("print_template") or design_from_fields(
		fields, template_doc.get("entry_for") or "Student"
	)
	body = frappe.render_template(design, Page(template_doc, entry_doc, fields, blank).context())
	deco = ""
	if theme == "soft":
		deco = "".join(_LEAF.format(pos=p) for p in ("tr", "tl", "br", "bl"))
	page_css = "<style>@page { size: A4 landscape; }</style>" if landscape else ""
	return (
		PRINT_STYLE
		+ page_css
		+ css_block(template_doc.get("print_css"))
		+ f'<div class="ms-page theme-{theme}{" landscape" if landscape else ""}">{deco}{body}</div>'
	)


# --- Fields <-> design -----------------------------------------------------------

# `{{ box("الوزن", "Number") }}` … up to three quoted arguments: label, type,
# options. `section("…")` is a heading.
CALL = re.compile(
	r"""\{\{\s*(field|box|inline|checks|section)\(\s*(["'])(.*?)\2"""
	r"""(?:\s*,\s*(["'])(.*?)\4)?"""
	r"""(?:\s*,\s*(["'])(.*?)\6)?\s*(?:,[^)]*)?\)\s*\}\}""",
	re.S,
)
VALUE = re.compile(r"""values(?:\.get\(\s*["']([a-z0-9_]+)["']|\.([a-z0-9_]+))""")


def _quote(text: str) -> str:
	return '"' + str(text).replace('"', "'") + '"'


def field_call(f: dict, helper: str = "box") -> str:
	"""The design markup for one field — carrying its type and options, so the
	design can be turned back into the same fields."""
	args = [_quote(f["label"] + (" *" if cint(f.get("reqd")) else ""))]
	fieldtype = f.get("fieldtype") or "Data"
	options = options_of(f)
	if fieldtype != "Data" or options:
		args.append(_quote(fieldtype))
	# A student table's columns and a text block's text hold `|` and `:` of
	# their own; they stay with the field, found again by its label.
	if options and fieldtype not in ("Text Block", "Student Table"):
		args.append(_quote("|".join(options)))
	return "{{ " + helper + "(" + ", ".join(args) + ") }}"


def _is_short(f: dict) -> bool:
	return f.get("fieldtype") in SHORT_TYPES and (f.get("width") or "half") != "full"


def design_from_fields(fields: list[dict], entry_for: str = "Student") -> str:
	"""A ready design: the letterhead, the facts at the top two to a row, a card
	per longer field, a section heading where the form has one, then the
	signatures and the footer."""
	meta = {
		"Student": [
			'{{ info("اسم الطالب/ة", student_name) }}',
			'{{ info("الصف والشعبة", section_label) }}',
			'{{ info("التاريخ", filled_date) }}',
		],
		"Section": ['{{ info("الصف والشعبة", section_label) }}', '{{ info("التاريخ", filled_date) }}'],
	}.get(entry_for, ['{{ info("التاريخ", filled_date) }}'])
	fields = [f for f in fields if (f.get("label") or "").strip()]
	i = 0
	while i < len(fields) and _is_short(fields[i]):
		meta.append(field_call(fields[i], "inline"))
		i += 1
	lines = ['<div class="ms-form">', "  {{ letterhead() }}", "  {{ card() }}{{ grid(2) }}"]
	lines += ["    " + m for m in meta]
	lines.append("  {{ endgrid() }}{{ endcard() }}")
	group: list[dict] = []

	def flush():
		if not group:
			return
		cols = 3 if any((g.get("width") or "") == "third" for g in group) else 2
		lines.append(f"  {{{{ card() }}}}{{{{ grid({cols}) }}}}")
		lines.extend("    " + field_call(g, "inline") for g in group)
		lines.append("  {{ endgrid() }}{{ endcard() }}")
		group.clear()

	for f in fields[i:]:
		ft = f.get("fieldtype") or "Data"
		if ft in LAYOUT:
			flush()
			lines.append("  {{ section(" + _quote(f["label"]) + ") }}")
		elif _is_short(f):
			group.append(f)
		else:
			flush()
			lines.append("  " + field_call(f, "box"))
	flush()
	lines += [
		'  {% if entry.notes and not blank %}{{ card("ملاحظات", "note") }}'
		'<div class="ans">{{ entry.notes }}</div>{{ endcard() }}{% endif %}',
		"  {{ signatures() }}",
		"  {{ footer() }}",
		"</div>",
	]
	return "\n".join(lines)
