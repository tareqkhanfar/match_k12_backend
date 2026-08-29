"""Every record carries the year and term it belongs to.

A record without a period is not merely untidy: the year switcher in the
header filters by these two fields, so an unstamped row is invisible on every
screen that honours the switcher, and a report over "last term" quietly
undercounts. Stamping is therefore done here, once, for every doctype — not
in each endpoint, where one forgotten line reintroduces the problem.

Three sources, in order of trust:

1. The record's own date. A behaviour note written today about an incident in
   the first term belongs to the first term, not to today's.
2. The parent it hangs off. A comment belongs to its post's period, a
   recipient row to its message's — reading the calendar again could put a
   reply in a different term than the message it answers.
3. The viewer's selected period, for records with no date of their own.
"""

import frappe

# The date that decides the period, per doctype. Where a record has several
# dates the earliest meaningful one wins: a loan belongs to the term it was
# issued in, not the term it happened to come back in.
DATE_SOURCE = {
	"MS Activity Enrolment": "enrolled_on",
	"MS Announcement": "posted_on",
	"MS Assignment Submission": "submitted_on",
	"MS Behaviour Record": "record_date",
	"MS Book Loan": "issue_date",
	"MS Health Visit": "visit_date",
	"MS Lesson Change": "schedule_date",
	"MS Message": "sent_on",
	"MS Quiz Attempt": "started_on",
	"MS Survey Response": "submitted_on",
	"MS Transport Assignment": "start_date",
}

# Children that must land in the same period as their parent.
INHERIT = {
	"MS Message Recipient": ("message", "MS Message"),
	"MS Post Comment": ("post", "MS Community Post"),
	"MS Post Like": ("post", "MS Community Post"),
	"MS Activity Enrolment": ("activity", "MS Activity"),
	"MS Assignment Submission": ("assignment", "MS Assignment"),
	"MS Quiz Attempt": ("quiz", "MS Quiz"),
	"MS Survey Response": ("survey", "MS Survey"),
}


def term_for_date(value) -> tuple[str | None, str | None]:
	"""The year and term a date falls inside, or (None, None).

	Read from the term calendar rather than from the school's current term, so
	backdated records file themselves where they actually belong.
	"""
	if not value:
		return None, None

	day = str(value)[:10]
	rows = frappe.get_all(
		"Academic Term",
		filters=[
			["term_start_date", "<=", day],
			["term_end_date", ">=", day],
		],
		fields=["name", "academic_year"],
		order_by="term_start_date desc",
		limit=1,
	)
	if rows:
		return rows[0].academic_year, rows[0].name

	# Between two terms — a holiday, or a date outside the calendar. The year
	# still holds even when no term does.
	years = frappe.get_all(
		"Academic Year",
		filters=[["year_start_date", "<=", day], ["year_end_date", ">=", day]],
		pluck="name",
		limit=1,
	)
	return (years[0] if years else None), None


def _from_parent(doc) -> tuple[str | None, str | None]:
	link = INHERIT.get(doc.doctype)
	if not link:
		return None, None
	field, parent_dt = link
	parent = doc.get(field)
	if not parent:
		return None, None
	row = frappe.db.get_value(
		parent_dt, parent, ["academic_year", "academic_term"], as_dict=True
	)
	if not row:
		return None, None
	return row.get("academic_year"), row.get("academic_term")


def resolve(doc) -> tuple[str | None, str | None]:
	"""The period this record belongs to, by the three rules above."""
	from match_schools.api.utils import get_default_academic_term, get_default_academic_year

	year, term = _from_parent(doc)
	if year:
		return year, term

	field = DATE_SOURCE.get(doc.doctype)
	if field:
		year, term = term_for_date(doc.get(field))
		if year:
			return year, term

	return get_default_academic_year(), get_default_academic_term()


def stamp(doc, method=None):
	"""Fill in the period before the record is written.

	Hooked on every doctype, so it must be cheap for the ones it does not
	touch: the name check rejects a core doctype before any metadata is read.
	An explicit value is never overwritten — a screen that knows better than
	the calendar (a promotion filed against next year, say) stays in charge.
	"""
	if not doc.doctype.startswith("MS "):
		return

	meta = frappe.get_meta(doc.doctype)
	has_year = meta.has_field("academic_year")
	has_term = meta.has_field("academic_term")
	if not (has_year or has_term):
		return
	if (not has_year or doc.get("academic_year")) and (not has_term or doc.get("academic_term")):
		return

	year, term = resolve(doc)
	if has_year and not doc.get("academic_year"):
		doc.academic_year = year
	if has_term and not doc.get("academic_term"):
		doc.academic_term = term
