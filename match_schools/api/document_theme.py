# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""One look for every document the school prints.

Certificates and reports used to carry their own inline CSS, so each drifted
from the others and none looked like it came from an institution. Everything
printable now renders through `document_shell()`: the same masthead with the
school's logo, the same typography, the same footer with an issue date and a
reference number.

Two constraints shape the CSS, and both are easy to forget:

  * wkhtmltopdf renders with an old WebKit. Flexbox and CSS grid are unreliable
    and silently collapse to a single column, so layout is done with tables.
  * These are Arabic documents. The page is RTL, numbers are forced LTR so a
    mark reads "18/20" rather than reversed, and the font stack starts with
    faces that actually carry Arabic glyphs.
"""

import frappe
from frappe.utils import flt, getdate, nowdate


def school_name() -> str:
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not company:
		return "Match Education"
	return frappe.db.get_value("Company", company, "company_name") or company


def school_logo() -> str | None:
	"""The school's own logo, as an absolute path wkhtmltopdf can read.

	A relative URL resolves against the filesystem when the PDF is rendered
	offline, so the site URL is prepended; a missing logo simply omits the
	image rather than printing a broken box.
	"""
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not company:
		return None
	logo = frappe.db.get_value("Company", company, "company_logo")
	if not logo:
		return None
	if logo.startswith("http"):
		return logo
	return f"{frappe.utils.get_url()}{logo}"


def _contact_line() -> str:
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not company:
		return ""
	row = frappe.db.get_value("Company", company, ["phone_no", "email"], as_dict=True) or {}
	parts = [p for p in (row.get("phone_no"), row.get("email")) if p]
	return " · ".join(parts)


BASE_CSS = """
  @page { size: A4; margin: 12mm 11mm 16mm 11mm; }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    direction: rtl;
    text-align: right;
    color: #1a202c;
    font-size: 10pt;
    line-height: 1.55;
    font-family: "Cairo", "Noto Naskh Arabic", "Amiri", "Tahoma", sans-serif;
  }

  /* Numbers stay left-to-right inside RTL text: "18/20", not "20/18". */
  .num { direction: ltr; unicode-bidi: embed; font-variant-numeric: tabular-nums; }

  /* --- Masthead ------------------------------------------------------- */
  .doc-head { width: 100%; border-collapse: collapse; }
  .doc-head td { vertical-align: middle; border: none; padding: 0; }
  .doc-logo { width: 74px; }
  .doc-logo img { width: 66px; height: 66px; object-fit: contain; }
  /* `dir="auto"` on the element plus isolation here: a Latin school name
     inside an RTL page otherwise has its brackets and punctuation reordered,
     printing "(Match Dev (Demo". */
  .doc-school { font-size: 17pt; font-weight: 800; letter-spacing: -0.2px;
                unicode-bidi: isolate; }
  .doc-contact { font-size: 8pt; color: #718096; margin-top: 2px; }
  .doc-rule { height: 3px; background: #1e40af; margin: 9px 0 0; }
  .doc-rule-thin { height: 1px; background: #cbd5e0; margin-top: 2px; }

  .doc-title-wrap { text-align: center; margin: 14px 0 4px; }
  .doc-title {
    display: inline-block;
    font-size: 14pt;
    font-weight: 800;
    color: #1e40af;
    border-bottom: 2px solid #1e40af;
    padding: 0 18px 4px;
  }
  .doc-subtitle { font-size: 9pt; color: #718096; margin-top: 5px; }

  /* --- Facts strip ---------------------------------------------------- */
  .facts { width: 100%; border-collapse: collapse; margin: 14px 0 4px;
           background: #f7fafc; border: 1px solid #e2e8f0; border-radius: 4px; }
  .facts td { padding: 8px 12px; font-size: 9pt; border: none;
              border-left: 1px solid #e2e8f0; }
  .facts td:last-child { border-left: none; }
  .facts .k { color: #718096; font-size: 8pt; display: block; }
  .facts .v { font-weight: 700; }

  /* --- Data tables ---------------------------------------------------- */
  table.data { width: 100%; border-collapse: collapse; margin-top: 12px;
               border: 1px solid #cbd5e0; }
  table.data thead tr { background: #1e40af; color: #fff; }
  table.data th { padding: 8px 7px; font-size: 9pt; font-weight: 700;
                  border: none; }
  table.data td { padding: 7px; border-bottom: 1px solid #e2e8f0;
                  text-align: center; font-size: 9.5pt; }
  table.data td.txt { text-align: right; font-weight: 600; }
  table.data tbody tr:nth-child(even) { background: #f7fafc; }
  table.data tbody tr:last-child td { border-bottom: none; }

  /* --- Summary panel -------------------------------------------------- */
  .panel { margin-top: 14px; border: 1.5px solid #1e40af; border-radius: 5px;
           overflow: hidden; }
  .panel-head { background: #1e40af; color: #fff; padding: 6px;
                text-align: center; font-weight: 700; font-size: 9pt; }
  .panel-body { width: 100%; border-collapse: collapse; }
  .panel-body td { padding: 11px 8px; text-align: center;
                   border-left: 1px solid #e2e8f0; border-bottom: none; }
  .panel-body td:last-child { border-left: none; }
  .panel-k { font-size: 8pt; color: #718096; }
  .panel-v { font-size: 14pt; font-weight: 800; margin-top: 3px; }

  /* --- Verdict pills -------------------------------------------------- */
  .pill { display: inline-block; padding: 2px 9px; border-radius: 10px;
          font-size: 8.5pt; font-weight: 700; }
  .pill-pass { background: #c6f6d5; color: #22543d; }
  .pill-fail { background: #fed7d7; color: #742a2a; }
  .pill-mid  { background: #feebc8; color: #7b341e; }

  .note { margin-top: 10px; font-size: 8pt; color: #718096; text-align: center; }

  /* --- Signatures ----------------------------------------------------- */
  .sigs { width: 100%; border: none; border-collapse: collapse; margin-top: 42px; }
  .sigs td { width: 33%; text-align: center; border: none; padding: 0 22px; }
  .sig-line { border-top: 1px solid #2d3748; padding-top: 6px;
              font-size: 8.5pt; color: #4a5568; }

  /* --- Footer --------------------------------------------------------- */
  .doc-foot { margin-top: 22px; border-top: 1px solid #e2e8f0; padding-top: 6px;
              font-size: 7.5pt; color: #a0aec0; }
  .doc-foot table { width: 100%; border-collapse: collapse; border: none; }
  .doc-foot td { border: none; padding: 0; }
"""


def document_shell(
	*,
	title: str,
	body: str,
	subtitle: str = "",
	facts: list[tuple[str, str]] | None = None,
	reference: str = "",
	extra_css: str = "",
	signatures: list[str] | None = None,
	note: str = "",
) -> str:
	"""Wrap a document body in the school's letterhead.

	`facts` is the strip under the title — student, class, year — as label/value
	pairs. `signatures` defaults to the two a school actually signs; pass an
	empty list for a document nobody signs.
	"""
	logo = school_logo()
	contact = _contact_line()
	issued = getdate(nowdate())

	logo_cell = (
		f'<td class="doc-logo"><img src="{frappe.utils.escape_html(logo)}"></td>'
		if logo
		else ""
	)

	facts_html = ""
	if facts:
		cells = "".join(
			f'<td><span class="k">{frappe.utils.escape_html(str(k))}</span>'
			f'<span class="v">{frappe.utils.escape_html(str(v))}</span></td>'
			for k, v in facts
			if v not in (None, "")
		)
		facts_html = f'<table class="facts"><tr>{cells}</tr></table>'

	signature_names = ["معلم الصف", "مدير المدرسة"] if signatures is None else signatures
	sig_html = ""
	if signature_names:
		cells = "".join(
			f'<td><div class="sig-line">{frappe.utils.escape_html(n)}</div></td>'
			for n in signature_names
		)
		sig_html = f'<table class="sigs"><tr>{cells}</tr></table>'

	note_html = f'<p class="note">{frappe.utils.escape_html(note)}</p>' if note else ""

	return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<style>{BASE_CSS}{extra_css}</style>
</head>
<body>
  <table class="doc-head">
    <tr>
      {logo_cell}
      <td>
        <div class="doc-school" dir="auto">{frappe.utils.escape_html(school_name())}</div>
        {f'<div class="doc-contact">{frappe.utils.escape_html(contact)}</div>' if contact else ''}
      </td>
    </tr>
  </table>
  <div class="doc-rule"></div>
  <div class="doc-rule-thin"></div>

  <div class="doc-title-wrap">
    <span class="doc-title">{frappe.utils.escape_html(title)}</span>
    {f'<div class="doc-subtitle">{frappe.utils.escape_html(subtitle)}</div>' if subtitle else ''}
  </div>

  {facts_html}
  {body}
  {note_html}
  {sig_html}

  <div class="doc-foot">
    <table>
      <tr>
        <td>صدرت بتاريخ {issued}</td>
        <td style="text-align:left">
          {frappe.utils.escape_html(reference) if reference else ''}
        </td>
      </tr>
    </table>
  </div>
</body>
</html>
"""


def verdict_pill(percent: float) -> str:
	"""A coloured verdict, consistent across every document.

	The wording comes from the school's own grade bands rather than thresholds
	repeated here, so a printed certificate and the screen can never disagree —
	a mark shown as "جيد جداً" in the portal printed as "ممتاز" before this.
	"""
	from match_schools.api.gradebook import grade_for

	value = flt(percent)
	band = grade_for(value)
	label = frappe.utils.escape_html(str(band.get("label") or ""))

	if value >= 65:
		tone = "pill-pass"
	elif value >= 50:
		tone = "pill-mid"
	else:
		tone = "pill-fail"
	return f'<span class="pill {tone}">{label}</span>'
