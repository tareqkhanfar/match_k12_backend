# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Export any table the frontend shows to Excel (xlsx) or PDF.

The frontend sends the columns it is currently displaying — including the
user's own show/hide and ordering choices — so the export always matches what
is on screen.
"""

import io
from datetime import date, datetime

import frappe
from frappe import _
from frappe.utils import flt, today
from frappe.utils.pdf import get_pdf

from match_k12.api.utils import (
	BACK_OFFICE,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_year,
	k12_endpoint,
	parse_json_arg,
)

# Only these datasets may be exported, and each maps to the endpoint that
# already enforces the caller's scope — so an export can never leak more than
# the matching screen would show.
DATASETS = {
	"students": ("match_k12.api.students", "list_students", "قائمة الطلاب"),
	"fees": ("match_k12.api.fees", "list_fees", "الرسوم المالية"),
	"grades": ("match_k12.api.academics", "list_grades", "الدرجات"),
	"exams": ("match_k12.api.academics", "list_exams", "جدول الامتحانات"),
	"classes": ("match_k12.api.academics", "list_classes", "الصفوف والشُعب"),
	"subjects": ("match_k12.api.academics", "list_subjects", "المواد الدراسية"),
	"teachers": ("match_k12.api.academics", "list_teachers", "المعلمون"),
	"assignments": ("match_k12.api.assignments", "list_assignments", "الواجبات"),
	"behaviour": ("match_k12.api.wellbeing", "list_behaviour", "السلوك والانضباط"),
	"books": ("match_k12.api.resources", "list_books", "المكتبة"),
	"loans": ("match_k12.api.resources", "list_loans", "إعارات الكتب"),
	"transport": ("match_k12.api.resources", "list_transport_assignments", "النقل المدرسي"),
}


def _fetch_rows(dataset: str, params: dict) -> list[dict]:
	"""Call the dataset's own list endpoint so permissions and scoping apply."""
	if dataset not in DATASETS:
		frappe.throw(_("Unknown dataset: {0}").format(dataset))

	module_path, fn_name, _label = DATASETS[dataset]
	module = frappe.get_module(module_path)
	fn = getattr(module, fn_name)

	# Ask for everything, not just the current page.
	params = dict(params or {})
	params.setdefault("page", 1)
	params.setdefault("page_size", 2000)

	result = fn(**params)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result

	if isinstance(data, dict) and "items" in data:
		return data["items"]
	if isinstance(data, list):
		return data
	return []


def _stringify(value) -> str:
	if value is None:
		return ""
	if isinstance(value, bool):
		return "نعم" if value else "لا"
	if isinstance(value, (datetime, date)):
		return str(value)
	if isinstance(value, (list, tuple)):
		return ", ".join(str(v) for v in value)
	if isinstance(value, dict):
		return frappe.as_json(value)
	return str(value)


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def export_excel(
	dataset: str,
	columns: str | list = None,
	filters: str | dict = None,
	title: str = None,
	persona: str = None,
):
	"""Build an .xlsx of the given dataset and return it as a download.

	`columns` is [{"fieldname": ..., "label": ...}, ...] in display order.
	"""
	columns = parse_json_arg(columns) or []
	filters = parse_json_arg(filters) or {}

	if not columns:
		return fail(
			message_en="No columns selected for export.",
			message_ar="لم يتم اختيار أي أعمدة للتصدير.",
		)

	rows = _fetch_rows(dataset, filters)
	label = title or DATASETS.get(dataset, (None, None, dataset))[2]

	try:
		from openpyxl import Workbook
		from openpyxl.styles import Alignment, Font, PatternFill
		from openpyxl.utils import get_column_letter
	except ImportError:
		return fail(
			message_en="openpyxl is not installed on the server.",
			message_ar="مكتبة التصدير غير مثبتة على الخادم.",
		)

	wb = Workbook()
	ws = wb.active
	ws.title = label[:31] or "Export"
	ws.sheet_view.rightToLeft = True  # Arabic reading order

	header_font = Font(bold=True, color="FFFFFF")
	header_fill = PatternFill("solid", fgColor="0550AE")
	centre = Alignment(horizontal="center", vertical="center")

	for col_idx, col in enumerate(columns, start=1):
		cell = ws.cell(row=1, column=col_idx, value=col.get("label") or col.get("fieldname"))
		cell.font = header_font
		cell.fill = header_fill
		cell.alignment = centre

	for row_idx, row in enumerate(rows, start=2):
		for col_idx, col in enumerate(columns, start=1):
			raw = row.get(col.get("fieldname"))
			# Keep numbers numeric so Excel can total them.
			if isinstance(raw, (int, float)) and not isinstance(raw, bool):
				ws.cell(row=row_idx, column=col_idx, value=raw)
			else:
				ws.cell(row=row_idx, column=col_idx, value=_stringify(raw))

	# Size columns to their content, within reason.
	for col_idx, col in enumerate(columns, start=1):
		longest = len(str(col.get("label") or ""))
		for row in rows[:200]:
			longest = max(longest, len(_stringify(row.get(col.get("fieldname")))))
		ws.column_dimensions[get_column_letter(col_idx)].width = min(max(longest + 2, 10), 48)

	ws.freeze_panes = "A2"
	ws.auto_filter.ref = (
		f"A1:{get_column_letter(len(columns))}{len(rows) + 1}" if rows else None
	)

	buffer = io.BytesIO()
	wb.save(buffer)

	filename = f"{label}-{today()}.xlsx"
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = buffer.getvalue()
	frappe.local.response.type = "binary"
	frappe.local.response[
		"content_type"
	] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER, ROLE_STUDENT, ROLE_PARENT)
def export_pdf(
	dataset: str,
	columns: str | list = None,
	filters: str | dict = None,
	title: str = None,
	orientation: str = "Landscape",
	persona: str = None,
):
	"""Render the dataset as an A4 PDF using a school-branded RTL template."""
	columns = parse_json_arg(columns) or []
	filters = parse_json_arg(filters) or {}

	if not columns:
		return fail(
			message_en="No columns selected for export.",
			message_ar="لم يتم اختيار أي أعمدة للتصدير.",
		)

	rows = _fetch_rows(dataset, filters)
	label = title or DATASETS.get(dataset, (None, None, dataset))[2]

	html = _render_table_html(label, columns, rows, filters)

	pdf = get_pdf(html, {"orientation": orientation or "Landscape"})

	frappe.local.response.filename = f"{label}-{today()}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _render_table_html(label: str, columns: list, rows: list, filters: dict) -> str:
	"""A print-ready RTL document: school header, filter strip, table, footer."""
	school = _school_header()

	head_cells = "".join(
		f"<th>{frappe.utils.escape_html(str(c.get('label') or c.get('fieldname')))}</th>"
		for c in columns
	)

	body_rows = []
	for idx, row in enumerate(rows, start=1):
		cells = [f'<td class="idx">{idx}</td>']
		for c in columns:
			value = _stringify(row.get(c.get("fieldname")))
			css = "num" if isinstance(row.get(c.get("fieldname")), (int, float)) else "txt"
			cells.append(f'<td class="{css}">{frappe.utils.escape_html(value)}</td>')
		body_rows.append("<tr>" + "".join(cells) + "</tr>")

	filter_chips = "".join(
		f'<span class="chip"><b>{frappe.utils.escape_html(str(k))}:</b> '
		f'{frappe.utils.escape_html(_stringify(v))}</span>'
		for k, v in (filters or {}).items()
		if v not in (None, "", [], {}) and k not in ("page", "page_size")
	)

	return f"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 10mm 8mm; }}
  * {{ box-sizing: border-box; font-family: "Cairo", "Noto Naskh Arabic", sans-serif; }}
  body {{ direction: rtl; text-align: right; color: #0d1117; font-size: 9pt; }}
  .head {{ display: flex; justify-content: space-between; align-items: flex-end;
           border-bottom: 2.5px solid #0d1117; padding-bottom: 8px; margin-bottom: 10px; }}
  .title {{ font-size: 15pt; font-weight: 700; color: #0550ae; }}
  .sub {{ font-size: 8pt; color: #4a5568; margin-top: 2px; }}
  .school {{ text-align: left; }}
  .school-name {{ font-size: 13pt; font-weight: 700; }}
  .chips {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 5px;
            padding: 6px 10px; margin-bottom: 10px; font-size: 8pt; }}
  .chip {{ margin-left: 14px; }}
  table {{ width: 100%; border-collapse: collapse; border: 1px solid #d0d7de; }}
  thead tr {{ background: #0550ae; color: #fff; }}
  th {{ padding: 5px 6px; font-size: 7.5pt; font-weight: 700; text-align: center;
        border-left: 1px solid rgba(255,255,255,.25); }}
  th:last-child {{ border-left: none; }}
  tbody tr:nth-child(even) {{ background: #f6f8fa; }}
  td {{ padding: 4px 6px; border-bottom: 1px solid #d0d7de;
        border-left: 1px solid #eaecef; text-align: center; }}
  td:last-child {{ border-left: none; }}
  td.txt {{ text-align: right; }}
  td.num {{ text-align: left; direction: ltr; font-variant-numeric: tabular-nums; }}
  td.idx {{ color: #4a5568; font-size: 7.5pt; }}
  tfoot td {{ font-weight: 700; background: #eef2f7; }}
  .foot {{ margin-top: 12px; border-top: 1px solid #d0d7de; padding-top: 5px;
           display: flex; justify-content: space-between; font-size: 7pt; color: #4a5568; }}
  .brand {{ font-weight: 700; color: #0550ae; }}
  thead {{ display: table-header-group; }}
  tr {{ page-break-inside: avoid; }}
</style>
</head>
<body>
  <div class="head">
    <div>
      <div class="title">{frappe.utils.escape_html(label)}</div>
      <div class="sub">عدد السجلات: {len(rows)}{_year_suffix()}</div>
    </div>
    <div class="school">
      <div class="school-name">{frappe.utils.escape_html(school)}</div>
      <div class="sub">تاريخ الطباعة: {frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M")}</div>
    </div>
  </div>

  {f'<div class="chips">{filter_chips}</div>' if filter_chips else ''}

  <table>
    <thead><tr><th style="width:4%">#</th>{head_cells}</tr></thead>
    <tbody>{''.join(body_rows) if body_rows else f'<tr><td colspan="{len(columns) + 1}">لا توجد بيانات</td></tr>'}</tbody>
  </table>

  <div class="foot">
    <span>{frappe.utils.escape_html(school)} — مستند داخلي</span>
    <span class="brand">Match Education</span>
  </div>
</body>
</html>
"""


def _school_header() -> str:
	company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not company:
		return "Match Education"
	return frappe.db.get_value("Company", company, "company_name") or company


def _year_suffix() -> str:
	year = get_default_academic_year()
	return f" — العام الدراسي {year}" if year else ""


@frappe.whitelist()
@k12_endpoint(ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
def export_report_card(student: str, academic_term: str = None, persona: str = None):
	"""A printable report card for one student."""
	from match_k12.api.academics import report_card

	result = report_card(student=student, academic_term=academic_term)
	data = result.get("data") if isinstance(result, dict) and "success" in result else result
	if not data or not data.get("subjects"):
		return fail(
			message_en="No results recorded for this student.",
			message_ar="لا توجد نتائج مسجّلة لهذا الطالب.",
		)

	html = _render_report_card_html(data)
	pdf = get_pdf(html, {"orientation": "Portrait"})

	frappe.local.response.filename = f"report-card-{student}-{today()}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _render_report_card_html(data: dict) -> str:
	school = _school_header()
	rows = "".join(
		f"""<tr>
			<td class="txt">{frappe.utils.escape_html(str(s['subject']))}</td>
			<td class="num">{flt(s['score']):g}</td>
			<td class="num">{flt(s['max']):g}</td>
			<td class="num">{flt(s['percentage']):g}%</td>
			<td>{frappe.utils.escape_html(str(s.get('grade') or '—'))}</td>
		</tr>"""
		for s in data["subjects"]
	)

	average = flt(data.get("average"))
	verdict = "ناجح" if average >= 50 else "راسب"

	return f"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 14mm 12mm; }}
  * {{ box-sizing: border-box; font-family: "Cairo", "Noto Naskh Arabic", sans-serif; }}
  body {{ direction: rtl; text-align: right; color: #0d1117; font-size: 10pt; }}
  .head {{ text-align: center; border-bottom: 2.5px solid #0d1117; padding-bottom: 10px; }}
  .school {{ font-size: 16pt; font-weight: 700; }}
  .doc {{ font-size: 13pt; font-weight: 700; color: #0550ae; margin-top: 4px; }}
  .meta {{ display: flex; justify-content: space-between; gap: 10px;
           background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
           padding: 10px 14px; margin: 14px 0; font-size: 9pt; }}
  table {{ width: 100%; border-collapse: collapse; border: 1px solid #d0d7de; }}
  thead tr {{ background: #0550ae; color: #fff; }}
  th {{ padding: 7px; font-size: 9pt; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #d0d7de; text-align: center; }}
  td.txt {{ text-align: right; }}
  td.num {{ direction: ltr; font-variant-numeric: tabular-nums; }}
  tbody tr:nth-child(even) {{ background: #f6f8fa; }}
  .summary {{ margin-top: 16px; border: 1.5px solid #0550ae; border-radius: 6px; overflow: hidden; }}
  .summary-head {{ background: #0550ae; color: #fff; padding: 6px; text-align: center;
                   font-weight: 700; font-size: 9pt; }}
  .summary-body {{ display: flex; }}
  .cell {{ flex: 1; padding: 10px; text-align: center; border-left: 1px solid #d0d7de; }}
  .cell:last-child {{ border-left: none; }}
  .cell-label {{ font-size: 8pt; color: #4a5568; }}
  .cell-value {{ font-size: 13pt; font-weight: 700; margin-top: 3px; }}
  .sigs {{ display: flex; justify-content: space-around; margin-top: 40px; }}
  .sig {{ text-align: center; min-width: 150px; }}
  .sig-line {{ border-top: 1px solid #0d1117; padding-top: 5px; font-size: 8.5pt; color: #4a5568; }}
</style>
</head>
<body>
  <div class="head">
    <div class="school">{frappe.utils.escape_html(school)}</div>
    <div class="doc">بطاقة الدرجات</div>
  </div>

  <div class="meta">
    <span><b>الطالب:</b> {frappe.utils.escape_html(str(data.get('student_name') or ''))}</span>
    <span><b>الرقم:</b> {frappe.utils.escape_html(str(data.get('student') or ''))}</span>
    <span><b>العام:</b> {frappe.utils.escape_html(str(data.get('academic_year') or '—'))}</span>
  </div>

  <table>
    <thead>
      <tr><th>المادة</th><th>الدرجة</th><th>من</th><th>النسبة</th><th>التقدير</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <div class="summary">
    <div class="summary-head">الملخص</div>
    <div class="summary-body">
      <div class="cell">
        <div class="cell-label">عدد المواد</div>
        <div class="cell-value">{len(data['subjects'])}</div>
      </div>
      <div class="cell">
        <div class="cell-label">المعدل العام</div>
        <div class="cell-value">{average:g}%</div>
      </div>
      <div class="cell">
        <div class="cell-label">النتيجة</div>
        <div class="cell-value">{verdict}</div>
      </div>
    </div>
  </div>

  <div class="sigs">
    <div class="sig"><div class="sig-line">مربي الصف</div></div>
    <div class="sig"><div class="sig-line">مدير المدرسة</div></div>
    <div class="sig"><div class="sig-line">ولي الأمر</div></div>
  </div>
</body>
</html>
"""
