# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class MSBellSchedule(Document):
	"""One school day: how many lessons, how long each is, when the break is.

	A school rarely runs one bell for everybody. The younger grades finish a
	lesson earlier and take their break before the fourth, the older ones take
	it after; a kindergarten may run five lessons where a secondary year runs
	eight. Each of those is a schedule here, and a grade or a single section
	is pointed at the one it follows.
	"""

	def validate(self):
		self.tidy_periods()
		self.check_times()
		self.keep_one_default()

	def tidy_periods(self):
		"""Order the rows by their time and number the lessons in sequence.

		A break carries no number: it is not a lesson, and numbering it would
		make the lesson after it "period 4" on screen and period 5 in the
		register.
		"""
		rows = sorted(self.periods or [], key=lambda p: str(p.from_time or ""))
		lesson = 0
		for index, row in enumerate(rows):
			row.idx = index + 1
			if cint(row.is_break):
				row.period_order = 0
				row.period_name = row.period_name or _("استراحة")
			else:
				lesson += 1
				row.period_order = lesson
				row.period_name = row.period_name or f"الحصة {lesson}"
		self.set("periods", rows)

	def check_times(self):
		teaching = [p for p in self.periods or [] if not cint(p.is_break)]
		if not teaching:
			frappe.throw(_("أضف حصة واحدة على الأقل."))

		last_end = None
		last_label = None
		for row in self.periods:
			if not row.from_time or not row.to_time:
				frappe.throw(_("اكتب وقت البداية والنهاية لكل حصة."))
			start, end = str(row.from_time), str(row.to_time)
			if start >= end:
				frappe.throw(
					_("{0}: وقت النهاية يجب أن يكون بعد وقت البداية.").format(row.period_name)
				)
			if last_end and start < last_end:
				frappe.throw(
					_("{0} تتداخل مع {1} — لا يمكن أن تعمل حصتان في الوقت نفسه.").format(
						row.period_name, last_label
					)
				)
			last_end, last_label = end, row.period_name

	def keep_one_default(self):
		"""Only one schedule stands in for a grade nobody assigned."""
		if not cint(self.is_default):
			return
		others = frappe.get_all(
			"MS Bell Schedule",
			filters={"is_default": 1, "name": ["!=", self.name]},
			pluck="name",
		)
		for other in others:
			frappe.db.set_value("MS Bell Schedule", other, "is_default", 0)

	def on_trash(self):
		"""A schedule in use is a school day someone's timetable depends on."""
		for doctype in ("Program", "Student Group"):
			used = frappe.get_all(
				doctype, filters={"ms_bell_schedule": self.name}, pluck="name", limit=3
			)
			if used:
				frappe.throw(
					_("هذا التوقيت مسنَد إلى: {0} — ألغِ الإسناد أولاً.").format("، ".join(used))
				)
