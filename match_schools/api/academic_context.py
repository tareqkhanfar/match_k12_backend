# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""The academic period every screen is read through, and the school calendar.

Two things live here because they answer the same question — "when are we?":

  * The academic year and term the user is looking at. Chosen in the header,
    remembered per user, and applied to every screen rather than each one
    filtering separately.
  * The holiday list. A school cannot hold a lesson, an exam or attendance on
    a holiday, so every screen that books a date checks `is_holiday` first.

A closed period is read-only for a student or a guardian, always. Staff may
still write: the back office unconditionally, a teacher only when the school
has switched it on. That rule lives in `assert_can_write` so no screen has to
re-derive it.
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate

from match_schools.api.utils import (
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
)

# Where a user's chosen period is remembered between sessions.
PREF_YEAR = "ms_academic_year"
PREF_TERM = "ms_academic_term"

# Whether a teacher may edit a closed term. Off unless the school turns it on.
SETTING_TEACHER_EDITS_CLOSED = "ms_teacher_can_edit_closed_period"


# --- School identity -------------------------------------------------------


def _company() -> dict:
	name = frappe.defaults.get_defaults().get("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if not name:
		return {}
	return (
		frappe.db.get_value(
			"Company",
			name,
			["name", "company_name", "company_logo", "email", "phone_no"],
			as_dict=True,
		)
		or {}
	)


def school_identity() -> dict:
	"""The school's own name and logo, for the header and the tab title.

	Falls back to the app's name so a fresh install still shows something
	sensible rather than an empty header.
	"""
	company = _company()
	return {
		"name": company.get("company_name") or "Match Education",
		"logo": company.get("company_logo"),
		"email": company.get("email"),
		"phone": company.get("phone_no"),
	}


# --- Holidays --------------------------------------------------------------


def _holiday_list_name(academic_year: str | None = None) -> str | None:
	"""The holiday list this school runs on.

	Education Settings does not carry one, so the company default is used and
	the only list is taken when there is exactly one — which is the common case
	for a single school.
	"""
	company = _company().get("name")
	if company:
		name = frappe.db.get_value("Company", company, "default_holiday_list")
		if name:
			return name

	lists = frappe.get_all("Holiday List", pluck="name", limit=2)
	return lists[0] if len(lists) == 1 else (lists[0] if lists else None)


@frappe.request_cache
def _holiday_map() -> dict[str, str]:
	"""date -> description, for the whole configured list."""
	name = _holiday_list_name()
	if not name:
		return {}
	rows = frappe.get_all(
		"Holiday",
		filters={"parent": name, "parenttype": "Holiday List"},
		fields=["holiday_date", "description", "weekly_off"],
		limit_page_length=0,
	)
	out: dict[str, str] = {}
	for r in rows:
		if not r.holiday_date:
			continue
		out[str(getdate(r.holiday_date))] = r.description or (
			"عطلة أسبوعية" if cint(r.weekly_off) else "عطلة"
		)
	return out


def is_holiday(date) -> bool:
	"""True when the school is closed on this date."""
	if not date:
		return False
	return str(getdate(date)) in _holiday_map()


def holiday_reason(date) -> str | None:
	if not date:
		return None
	return _holiday_map().get(str(getdate(date)))


def assert_not_holiday(date, what: str = "هذا الإجراء"):
	"""Refuse to book anything on a day the school is closed.

	Called by scheduling, exams and attendance rather than each of them
	carrying its own copy of the rule.
	"""
	reason = holiday_reason(date)
	if reason:
		frappe.throw(
			_("{0} غير ممكن بتاريخ {1} — {2}.").format(what, str(getdate(date)), reason),
			frappe.ValidationError,
		)


@frappe.whitelist()
@ms_endpoint()
def holidays(academic_year: str = None, persona: str = None):
	"""The school calendar: every holiday, newest first."""
	name = _holiday_list_name(academic_year)
	if not name:
		return {"list": None, "holidays": [], "range": None, "canEdit": persona in BACK_OFFICE}

	head = frappe.db.get_value(
		"Holiday List",
		name,
		["name", "holiday_list_name", "from_date", "to_date", "weekly_off", "total_holidays"],
		as_dict=True,
	)
	rows = frappe.get_all(
		"Holiday",
		filters={"parent": name, "parenttype": "Holiday List"},
		fields=["name", "holiday_date", "description", "weekly_off"],
		order_by="holiday_date",
		limit_page_length=0,
	)

	today = getdate(nowdate())
	return {
		"list": head.name,
		"listName": head.holiday_list_name,
		"range": {"from": str(head.from_date or ""), "to": str(head.to_date or "")},
		"weeklyOff": head.weekly_off,
		"canEdit": persona in BACK_OFFICE,
		"holidays": [
			{
				"id": r.name,
				"date": str(r.holiday_date or ""),
				"description": r.description,
				"weeklyOff": bool(r.weekly_off),
				"past": bool(r.holiday_date and getdate(r.holiday_date) < today),
			}
			for r in rows
		],
	}


@frappe.whitelist()
@ms_endpoint()
def upcoming_holidays(days: int = 30, persona: str = None):
	"""Holidays inside the next few weeks, for the banner and notifications."""
	days = min(max(cint(days) or 30, 1), 180)
	today = getdate(nowdate())
	out = []
	for date_str, reason in sorted(_holiday_map().items()):
		date = getdate(date_str)
		delta = (date - today).days
		if 0 <= delta <= days:
			out.append(
				{
					"date": date_str,
					"reason": reason,
					"inDays": delta,
					"isToday": delta == 0,
				}
			)
	return {"holidays": out, "today": str(today), "todayIsHoliday": is_holiday(today),
	        "todayReason": holiday_reason(today)}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def save_holiday(
	date: str = None,
	description: str = None,
	holiday: str = None,
	persona: str = None,
):
	"""Add a holiday, or edit one that exists."""
	if not date:
		return fail(
			message_en="A date is required.",
			message_ar="يجب تحديد التاريخ.",
		)

	name = _holiday_list_name()
	if not name:
		return fail(
			message_en="No holiday list is configured for this school.",
			message_ar="لا توجد قائمة عطل معرّفة للمدرسة.",
		)

	doc = frappe.get_doc("Holiday List", name)

	if holiday:
		row = next((r for r in doc.holidays if r.name == holiday), None)
		if not row:
			return fail(
				message_en="That holiday no longer exists.",
				message_ar="هذه العطلة لم تعد موجودة.",
			)
		row.holiday_date = getdate(date)
		row.description = description or row.description
	else:
		clash = next(
			(r for r in doc.holidays if str(getdate(r.holiday_date)) == str(getdate(date))),
			None,
		)
		if clash:
			return fail(
				message_en="That date is already a holiday.",
				message_ar="هذا التاريخ مسجّل كعطلة بالفعل.",
			)
		doc.append(
			"holidays",
			{"holiday_date": getdate(date), "description": description or "عطلة"},
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"list": doc.name, "message_ar": "تم حفظ العطلة."}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*BACK_OFFICE)
def delete_holiday(holiday: str = None, persona: str = None):
	if not holiday:
		return fail(
			message_en="A holiday is required.",
			message_ar="يجب تحديد العطلة.",
		)

	name = _holiday_list_name()
	doc = frappe.get_doc("Holiday List", name)
	before = len(doc.holidays)
	doc.holidays = [r for r in doc.holidays if r.name != holiday]
	if len(doc.holidays) == before:
		return fail(
			message_en="That holiday no longer exists.",
			message_ar="هذه العطلة لم تعد موجودة.",
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"message_ar": "تم حذف العطلة."}


# --- The chosen academic period --------------------------------------------


def _term_window(term: str | None) -> tuple[str | None, str | None]:
	if not term:
		return None, None
	row = frappe.db.get_value(
		"Academic Term", term, ["term_start_date", "term_end_date"], as_dict=True
	)
	if not row:
		return None, None
	return (
		str(row.term_start_date) if row.term_start_date else None,
		str(row.term_end_date) if row.term_end_date else None,
	)


def _year_window(year: str | None) -> tuple[str | None, str | None]:
	if not year:
		return None, None
	row = frappe.db.get_value(
		"Academic Year", year, ["year_start_date", "year_end_date"], as_dict=True
	)
	if not row:
		return None, None
	return (
		str(row.year_start_date) if row.year_start_date else None,
		str(row.year_end_date) if row.year_end_date else None,
	)


def is_period_closed(academic_year: str | None, academic_term: str | None) -> bool:
	"""A period is closed once its end date has passed.

	The term decides when there is one, because a school can be inside the
	current year but past the first term.
	"""
	today = getdate(nowdate())
	_, term_end = _term_window(academic_term)
	if term_end:
		return getdate(term_end) < today
	_, year_end = _year_window(academic_year)
	if year_end:
		return getdate(year_end) < today
	return False


def teacher_may_edit_closed() -> bool:
	return bool(
		frappe.db.get_single_value("Education Settings", SETTING_TEACHER_EDITS_CLOSED)
	)


def can_write(persona: str, academic_year: str | None, academic_term: str | None) -> bool:
	"""Whether this persona may change data in the chosen period."""
	if persona in (ROLE_ADMIN, ROLE_SECRETARY):
		return True
	if not is_period_closed(academic_year, academic_term):
		return True
	if persona == ROLE_TEACHER:
		return teacher_may_edit_closed()
	# Students and guardians never write into a closed period.
	return False


def assert_can_write(
	persona: str, academic_year: str | None = None, academic_term: str | None = None
):
	"""Guard for every write that belongs to an academic period."""
	year = academic_year or current_period().get("academicYear")
	term = academic_term or current_period().get("academicTerm")
	if can_write(persona, year, term):
		return
	frappe.throw(
		_("الفصل الدراسي مغلق ولا يمكن التعديل عليه."),
		frappe.PermissionError,
	)


@frappe.request_cache
def current_period() -> dict:
	"""The period this user is working in.

	Their own choice when they have made one and it still exists; otherwise the
	school's current year and term.
	"""
	year = frappe.defaults.get_user_default(PREF_YEAR) or None
	term = frappe.defaults.get_user_default(PREF_TERM) or None

	if year and not frappe.db.exists("Academic Year", year):
		year = None
	if term and not frappe.db.exists("Academic Term", term):
		term = None
	# A term from a different year is not a valid combination.
	if term and year:
		if frappe.db.get_value("Academic Term", term, "academic_year") != year:
			term = None

	if not year:
		year = get_default_academic_year()
	if not term:
		default_term = get_default_academic_term()
		if default_term and (
			not year
			or frappe.db.get_value("Academic Term", default_term, "academic_year") == year
		):
			term = default_term

	return {"academicYear": year, "academicTerm": term}


@frappe.whitelist()
@ms_endpoint()
def get_context(persona: str = None):
	"""What the header needs: the school, the period, and the year/term lists."""
	period = current_period()
	year = period["academicYear"]
	term = period["academicTerm"]

	years = frappe.get_all(
		"Academic Year",
		fields=["name", "year_start_date", "year_end_date"],
		order_by="year_start_date desc",
		limit_page_length=0,
	)
	terms = frappe.get_all(
		"Academic Term",
		fields=["name", "academic_year", "term_name", "term_start_date", "term_end_date"],
		order_by="term_start_date",
		limit_page_length=0,
	)

	closed = is_period_closed(year, term)
	today = getdate(nowdate())

	return {
		"school": school_identity(),
		"academicYear": year,
		"academicTerm": term,
		"years": [
			{
				"name": y.name,
				"from": str(y.year_start_date or ""),
				"to": str(y.year_end_date or ""),
				"closed": bool(y.year_end_date and getdate(y.year_end_date) < today),
			}
			for y in years
		],
		"terms": [
			{
				"name": t.name,
				"label": t.term_name or t.name,
				"academicYear": t.academic_year,
				"from": str(t.term_start_date or ""),
				"to": str(t.term_end_date or ""),
				"closed": bool(t.term_end_date and getdate(t.term_end_date) < today),
			}
			for t in terms
		],
		"closed": closed,
		"canWrite": can_write(persona, year, term),
		# So a screen can explain *why* it is read-only rather than just
		# disabling its buttons.
		"readOnlyReason": (
			"الفصل الدراسي مغلق ولا يمكن التعديل عليه." if closed and not can_write(persona, year, term) else None
		),
		"today": str(today),
		"todayIsHoliday": is_holiday(today),
		"todayHolidayReason": holiday_reason(today),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint()
def set_period(academic_year: str = None, academic_term: str = None, persona: str = None):
	"""Remember the period this user wants to work in.

	Stored as a user default so the choice survives a reload and a new session,
	which is what "the whole system follows my choice" has to mean.
	"""
	if academic_year and not frappe.db.exists("Academic Year", academic_year):
		return fail(
			message_en="That academic year does not exist.",
			message_ar="العام الدراسي غير موجود.",
		)
	if academic_term:
		owner_year = frappe.db.get_value("Academic Term", academic_term, "academic_year")
		if not owner_year:
			return fail(
				message_en="That academic term does not exist.",
				message_ar="الفصل الدراسي غير موجود.",
			)
		if academic_year and owner_year != academic_year:
			return fail(
				message_en="That term belongs to a different academic year.",
				message_ar="هذا الفصل يتبع عاماً دراسياً آخر.",
			)
		if not academic_year:
			academic_year = owner_year

	# Stored against the user, not globally. `frappe.db.set_default` writes the
	# shared `__default` row, which would make one user's choice change the
	# period for everyone — and would not be read back, because `get_default`
	# looks at the user's own row first.
	#
	# No explicit cache clear: frappe.defaults.set_default already invalidates
	# the user's defaults cache, and frappe.defaults has no `clear_cache`.
	frappe.defaults.set_user_default(PREF_YEAR, academic_year or "", user=frappe.session.user)
	frappe.defaults.set_user_default(PREF_TERM, academic_term or "", user=frappe.session.user)
	frappe.db.commit()

	# `current_period` is request-cached, so anything computed after this call
	# in the same request would still see the previous choice. The values below
	# are therefore derived from the arguments, not from current_period().

	closed = is_period_closed(academic_year, academic_term)
	return {
		"academicYear": academic_year,
		"academicTerm": academic_term,
		"closed": closed,
		"canWrite": can_write(persona, academic_year, academic_term),
		"message_ar": "تم تغيير الفترة الدراسية.",
	}
