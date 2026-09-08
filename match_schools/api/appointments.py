"""Office hours, and the appointments booked into them.

A parent who wants ten minutes with a teacher currently phones the office and
the office walks down a corridor. This turns that into a booking: staff
declare when they are available, families see the free slots, and the meeting
lands in both diaries.

Availability is computed, never stored. A stored slot table would drift the
moment a lesson moved or somebody booked, and two people would be promised the
same ten minutes. Three things are subtracted from a declared window, in
order: lessons the member of staff is actually teaching at that hour, the
appointments already booked, and anything in the past.

Who may book whom follows the same rule as the mailbox: a family may book the
teachers who teach their children, and the office. A teacher may book nobody —
they are the ones being booked — but they see and answer their own queue.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, getdate, now, nowdate

from match_schools.api.utils import (
	apply_period,
	fail,
	hhmm,
	instructor_user,
	ms_endpoint,
	resolve_scope,
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
FAMILY = (ROLE_STUDENT, ROLE_PARENT)
ALL_ROLES = (*STAFF, *FAMILY)

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_AR = {
	"Sunday": "الأحد",
	"Monday": "الإثنين",
	"Tuesday": "الثلاثاء",
	"Wednesday": "الأربعاء",
	"Thursday": "الخميس",
	"Friday": "الجمعة",
	"Saturday": "السبت",
}
STATUS_AR = {
	"Requested": "بانتظار الموافقة",
	"Confirmed": "مؤكَّد",
	"Declined": "معتذَر عنه",
	"Cancelled": "ملغى",
	"Completed": "تم",
}
STATUS_TONE = {
	"Requested": "warning",
	"Confirmed": "success",
	"Declined": "danger",
	"Cancelled": "muted",
	"Completed": "muted",
}

# How far ahead a family may book. Far enough to be useful, near enough that
# the timetable it was booked against is still the timetable that will run.
HORIZON_DAYS = 30
MIN_SLOT = 5
MAX_SLOT = 120


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _minutes(value) -> int:
	"""A Time column as minutes past midnight.

	Frappe hands back a timedelta for a Time field, and str() on it drops the
	leading zero, so parsing the string is not safe.
	"""
	if value is None:
		return 0
	if hasattr(value, "total_seconds"):
		return int(value.total_seconds() // 60)
	text = str(value)
	parts = text.split(":")
	return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)


def _clock(minutes: int) -> str:
	return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _overlaps(a_from: int, a_to: int, b_from: int, b_to: int) -> bool:
	return a_from < b_to and b_from < a_to


# ---------------------------------------------------------------------------
# Who may book whom
# ---------------------------------------------------------------------------


def _instructor_user(instructor: str) -> str | None:
	"""موحَّد مع بقية النظام — انظر `instructor_user`."""
	return instructor_user(instructor)


def _office_users() -> list[str]:
	ids = {
		r.parent
		for r in frappe.get_all(
			"Has Role",
			filters={"role": ["in", ["MS Admin", "MS Secretary"]], "parenttype": "User"},
			fields=["parent"],
		)
	}
	if not ids:
		return []
	return frappe.get_all(
		"User", filters={"name": ["in", list(ids)], "enabled": 1}, pluck="name"
	)


def bookable_staff(persona: str) -> list[str] | None:
	"""Accounts this caller may request a meeting with.

	`None` means no restriction — the office may book anyone. A family is
	limited to the staff who actually teach their children, plus the office,
	which is the same shape as the messaging rule.
	"""
	if persona in BACK_OFFICE:
		return None

	scope = resolve_scope(persona)
	users: set[str] = set(_office_users())

	if persona in FAMILY:
		students = scope.get("students") or []
		groups = frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students or [""]], "active": 1},
			pluck="parent",
		)
		# شُعب الفصل المختار وحدها: معلّم العام الماضي لا تُحجز عنده مواعيد.
		if groups:
			groups = frappe.get_all(
				"Student Group",
				filters=apply_period({"name": ["in", groups]}, "Student Group"),
				pluck="name",
			)
		instructors = set(
			frappe.get_all(
				"Student Group Instructor",
				filters={"parent": ["in", groups or [""]], "parenttype": "Student Group"},
				pluck="instructor",
			)
		)
		instructors |= {
			r.instructor
			for r in frappe.get_all(
				"MS Timetable Slot",
				filters={"student_group": ["in", groups or [""]], "active": 1},
				fields=["instructor"],
				limit_page_length=0,
			)
			if r.instructor
		}
		# والحصص المولَّدة مصدرٌ ثالث: معلّمٌ يظهر في جدول الطالب يجب أن
		# يكون قابلاً للحجز عنده، مهما كانت طريقة ربطه بالشعبة.
		instructors |= {
			i
			for i in frappe.get_all(
				"Course Schedule",
				filters={"student_group": ["in", groups or [""]], "docstatus": ["<", 2]},
				pluck="instructor",
				limit_page_length=0,
			)
			if i
		}
		for i in instructors:
			user = _instructor_user(i)
			if user:
				users.add(user)

	users.discard(frappe.session.user)
	return sorted(users)


# ---------------------------------------------------------------------------
# Office hours
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*STAFF)
def my_office_hours(staff: str = None, persona: str = None):
	"""The caller's weekly availability, or a colleague's if the office asks."""
	user = staff if (staff and persona in BACK_OFFICE) else frappe.session.user

	filters = {"staff_user": user}
	apply_period(filters, "MS Office Hour")
	rows = frappe.get_all(
		"MS Office Hour",
		filters=filters,
		fields=[
			"name", "day", "from_time", "to_time", "slot_minutes", "location",
			"is_active", "allow_students", "allow_guardians", "notes",
		],
		limit_page_length=0,
	)

	out = [
		{
			"id": r.name,
			"day": r.day,
			"day_label": DAY_AR.get(r.day, r.day),
			"from_time": hhmm(r.from_time),
			"to_time": hhmm(r.to_time),
			"slot_minutes": cint(r.slot_minutes) or 15,
			"location": r.location,
			"is_active": bool(cint(r.is_active)),
			"allow_students": bool(cint(r.allow_students)),
			"allow_guardians": bool(cint(r.allow_guardians)),
			"notes": r.notes,
		}
		for r in rows
	]
	out.sort(key=lambda r: (DAYS.index(r["day"]) if r["day"] in DAYS else 9, r["from_time"]))

	return {
		"staff_user": user,
		"hours": out,
		"days": [{"key": d, "label": DAY_AR[d]} for d in DAYS],
		"teaching": _teaching_grid(user),
	}


def _teaching_grid(user: str) -> list[dict]:
	"""When this member of staff is already in a lesson.

	Shown beside the office-hours editor so a teacher does not declare
	themselves free during a lesson they teach — and used by `availability` to
	drop such a slot even if they did.
	"""
	instructors = frappe.get_all("Instructor", filters={"ms_user": user}, pluck="name")
	employees = frappe.get_all("Employee", filters={"user_id": user}, pluck="name")
	if employees:
		instructors += frappe.get_all(
			"Instructor", filters={"employee": ["in", employees]}, pluck="name"
		)
	if not instructors:
		return []

	filters = {"instructor": ["in", list(set(instructors))], "active": 1}
	apply_period(filters, "MS Timetable Slot")
	rows = frappe.get_all(
		"MS Timetable Slot",
		filters=filters,
		fields=["day", "from_time", "to_time", "course", "student_group"],
		limit_page_length=0,
	)
	return [
		{
			"day": r.day,
			"from_time": hhmm(r.from_time),
			"to_time": hhmm(r.to_time),
			"course": r.course,
			"student_group": r.student_group,
		}
		for r in rows
	]


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*STAFF)
def save_office_hours(payload: str | dict = None, persona: str = None):
	"""Replace the caller's weekly availability with the grid they submitted.

	Sent and stored whole rather than row by row: the screen is a weekly grid,
	and a half-applied grid is a teacher promising hours they did not mean.
	"""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	staff = data.get("staff_user")
	user = staff if (staff and persona in BACK_OFFICE) else frappe.session.user

	rows = data.get("hours")
	if rows is None:
		return fail(message_en="Nothing to save.", message_ar="لا يوجد ما يُحفظ.")
	if len(rows) > 40:
		return fail(
			message_en="Too many windows.",
			message_ar="عدد الفترات كبير جداً.",
		)

	cleaned = []
	for row in rows:
		day = (row or {}).get("day")
		if day not in DAYS:
			return fail(message_en="Unknown day.", message_ar="يوم غير معروف.")
		start, end = _minutes(row.get("from_time")), _minutes(row.get("to_time"))
		if end <= start:
			return fail(
				message_en="The end of a window must be after its start.",
				message_ar=f"وقت النهاية يجب أن يكون بعد البداية ({DAY_AR[day]}).",
			)
		slot = cint(row.get("slot_minutes")) or 15
		if slot < MIN_SLOT or slot > MAX_SLOT:
			return fail(
				message_en=f"A slot is between {MIN_SLOT} and {MAX_SLOT} minutes.",
				message_ar=f"مدة الموعد بين {MIN_SLOT} و{MAX_SLOT} دقيقة.",
			)
		if slot > end - start:
			return fail(
				message_en="The slot is longer than the window.",
				message_ar=f"مدة الموعد أطول من الفترة نفسها ({DAY_AR[day]}).",
			)
		cleaned.append({**row, "day": day, "_from": start, "_to": end, "_slot": slot})

	# Two overlapping windows on one day would offer the same minute twice.
	for day in DAYS:
		same = sorted([c for c in cleaned if c["day"] == day], key=lambda c: c["_from"])
		for a, b in zip(same, same[1:]):
			if _overlaps(a["_from"], a["_to"], b["_from"], b["_to"]):
				return fail(
					message_en="Two windows on the same day overlap.",
					message_ar=f"فترتان متداخلتان في يوم {DAY_AR[day]}.",
				)

	existing = frappe.get_all("MS Office Hour", filters={"staff_user": user}, pluck="name")

	# A window with appointments already booked into it is not deleted, only
	# switched off: the bookings must keep pointing at something.
	booked = set(
		frappe.get_all(
			"MS Appointment",
			filters={
				"office_hour": ["in", existing or [""]],
				"status": ["in", ["Requested", "Confirmed"]],
				"appointment_date": [">=", nowdate()],
			},
			pluck="office_hour",
		)
	)
	for name in existing:
		if name in booked:
			frappe.db.set_value("MS Office Hour", name, "is_active", 0, update_modified=False)
		else:
			frappe.delete_doc("MS Office Hour", name, ignore_permissions=True, force=True)

	full_name = frappe.db.get_value("User", user, "full_name")
	for row in cleaned:
		frappe.get_doc(
			{
				"doctype": "MS Office Hour",
				"staff_user": user,
				"staff_name": full_name,
				"day": row["day"],
				"from_time": _clock(row["_from"]),
				"to_time": _clock(row["_to"]),
				"slot_minutes": row["_slot"],
				"location": (row.get("location") or "")[:140],
				"is_active": 1 if cint(row.get("is_active", 1)) else 0,
				"allow_students": 1 if cint(row.get("allow_students", 1)) else 0,
				"allow_guardians": 1 if cint(row.get("allow_guardians", 1)) else 0,
				"notes": row.get("notes"),
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()
	return {
		"success": True,
		"data": {"count": len(cleaned)},
		"message_en": "Office hours saved.",
		"message_ar": "تم حفظ الساعات المكتبية.",
	}


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def staff_directory(search: str = None, persona: str = None):
	"""Staff the caller may book, and whether each has published any hours."""
	allowed = bookable_staff(persona)
	filters = {"enabled": 1}
	if allowed is not None:
		if not allowed:
			return {"staff": []}
		filters["name"] = ["in", allowed]
	if search:
		filters["full_name"] = ["like", f"%{search.strip()}%"]

	users = frappe.get_all(
		"User", filters=filters, fields=["name", "full_name"], limit_page_length=200
	)
	if not users:
		return {"staff": []}

	names = [u.name for u in users]
	published = {
		r.staff_user
		for r in frappe.get_all(
			"MS Office Hour",
			filters={"staff_user": ["in", names], "is_active": 1},
			fields=["staff_user"],
			limit_page_length=0,
		)
	}

	# What each teacher teaches the caller's children, so a parent picking from
	# a list of twenty names knows which one is the maths teacher.
	subjects: dict[str, set] = {}
	if persona in FAMILY:
		students = resolve_scope(persona).get("students") or []
		groups = frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students or [""]], "active": 1},
			pluck="parent",
		)
		for row in frappe.get_all(
			"MS Timetable Slot",
			filters={"student_group": ["in", groups or [""]], "active": 1},
			fields=["instructor", "course"],
			limit_page_length=0,
		):
			user = _instructor_user(row.instructor)
			if user and row.course:
				subjects.setdefault(user, set()).add(row.course)

	return {
		"staff": sorted(
			[
				{
					"user": u.name,
					"name": u.full_name or u.name,
					"has_hours": u.name in published,
					"subjects": sorted(subjects.get(u.name, [])),
				}
				for u in users
			],
			key=lambda r: (not r["has_hours"], r["name"]),
		)
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def availability(staff: str = None, from_date: str = None, days: int = 14, persona: str = None):
	"""Free slots for one member of staff over the coming days.

	Computed from the declared windows minus lessons, minus bookings, minus
	the past. Nothing here is stored: a stored slot table drifts the moment a
	lesson moves, and two families get promised the same ten minutes.
	"""
	if not staff:
		return fail(message_en="Choose a member of staff.", message_ar="اختر الموظف.")

	allowed = bookable_staff(persona)
	if allowed is not None and staff not in allowed:
		frappe.throw(_("You cannot book this person."), frappe.PermissionError)

	days = min(max(cint(days) or 14, 1), HORIZON_DAYS)
	start = getdate(from_date or nowdate())
	if start < getdate(nowdate()):
		start = getdate(nowdate())

	windows = frappe.get_all(
		"MS Office Hour",
		filters={"staff_user": staff, "is_active": 1},
		fields=[
			"name", "day", "from_time", "to_time", "slot_minutes", "location",
			"allow_students", "allow_guardians",
		],
		limit_page_length=0,
	)
	if not windows:
		return {
			"staff": staff,
			"staff_name": frappe.db.get_value("User", staff, "full_name"),
			"days": [],
			"has_hours": False,
		}

	lessons = _teaching_grid(staff)
	last = add_to_date(start, days=days - 1)
	taken = frappe.get_all(
		"MS Appointment",
		filters={
			"staff_user": staff,
			"status": ["in", ["Requested", "Confirmed"]],
			"appointment_date": ["between", [start, last]],
		},
		fields=["appointment_date", "from_time", "to_time"],
		limit_page_length=0,
	)
	booked: dict[str, list[tuple[int, int]]] = {}
	for row in taken:
		booked.setdefault(str(row.appointment_date), []).append(
			(_minutes(row.from_time), _minutes(row.to_time))
		)

	now_dt = get_datetime(now())
	out = []
	for offset in range(days):
		date = add_to_date(start, days=offset)
		key = str(getdate(date))
		weekday = DAYS[(getdate(date).weekday() + 1) % 7]
		slots = []

		for window in [w for w in windows if w.day == weekday]:
			if persona == ROLE_STUDENT and not cint(window.allow_students):
				continue
			if persona == ROLE_PARENT and not cint(window.allow_guardians):
				continue

			step = cint(window.slot_minutes) or 15
			cursor, end = _minutes(window.from_time), _minutes(window.to_time)
			while cursor + step <= end:
				slot_end = cursor + step
				clash = any(
					_overlaps(cursor, slot_end, _minutes(x["from_time"]), _minutes(x["to_time"]))
					for x in lessons
					if x["day"] == weekday
				) or any(
					_overlaps(cursor, slot_end, b_from, b_to)
					for b_from, b_to in booked.get(key, [])
				)
				in_past = get_datetime(f"{key} {_clock(cursor)}:00") <= now_dt
				if not clash and not in_past:
					slots.append(
						{
							"from_time": _clock(cursor),
							"to_time": _clock(slot_end),
							"office_hour": window.name,
							"location": window.location,
						}
					)
				cursor = slot_end

		if slots:
			out.append(
				{
					"date": key,
					"day": weekday,
					"day_label": DAY_AR[weekday],
					"slots": slots,
				}
			)

	return {
		"staff": staff,
		"staff_name": frappe.db.get_value("User", staff, "full_name"),
		"days": out,
		"has_hours": True,
	}


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------


def _row(doc, viewer: str) -> dict:
	return {
		"id": doc.name,
		"staff_user": doc.staff_user,
		"staff_name": doc.staff_name or doc.staff_user,
		"status": doc.status,
		"status_label": STATUS_AR.get(doc.status, doc.status),
		"status_tone": STATUS_TONE.get(doc.status, "muted"),
		"date": str(doc.appointment_date or ""),
		"day_label": (
			DAY_AR[DAYS[(getdate(doc.appointment_date).weekday() + 1) % 7]]
			if doc.appointment_date
			else ""
		),
		"from_time": hhmm(doc.from_time),
		"to_time": hhmm(doc.to_time),
		"requested_by": doc.requested_by,
		"requester_name": doc.requester_name or doc.requested_by,
		"requester_role": doc.requester_role,
		"student": doc.student,
		"student_name": doc.student_name,
		"student_group": doc.student_group,
		"subject": doc.subject,
		"notes": doc.notes,
		"location": doc.location,
		"decline_reason": doc.decline_reason,
		"decided_on": str(doc.decided_on or ""),
		"mine": doc.requested_by == viewer,
		"is_staff": doc.staff_user == viewer,
		"can_decide": doc.staff_user == viewer and doc.status == "Requested",
		"can_cancel": (
			doc.requested_by == viewer and doc.status in ("Requested", "Confirmed")
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def book(payload: str | dict = None, persona: str = None):
	"""Request one slot.

	The slot is re-checked here against the same rules `availability` used.
	The screen showed a list a moment ago; between then and now somebody else
	may have taken the slot, and a booking screen that trusts its own stale
	list is how two families arrive at once.
	"""
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	staff = data.get("staff_user")
	date = data.get("date")
	from_time = data.get("from_time")
	subject = (data.get("subject") or "").strip()

	if not (staff and date and from_time):
		return fail(message_en="Choose a slot.", message_ar="اختر موعداً.")
	if not subject:
		return fail(message_en="A reason is required.", message_ar="سبب الموعد مطلوب.")

	allowed = bookable_staff(persona)
	if allowed is not None and staff not in allowed:
		frappe.throw(_("You cannot book this person."), frappe.PermissionError)

	when = getdate(date)
	if when < getdate(nowdate()):
		return fail(message_en="That date has passed.", message_ar="هذا التاريخ قد مضى.")
	if when > getdate(add_to_date(nowdate(), days=HORIZON_DAYS)):
		return fail(
			message_en=f"Bookings open {HORIZON_DAYS} days ahead.",
			message_ar=f"الحجز متاح خلال {HORIZON_DAYS} يوماً القادمة فقط.",
		)

	weekday = DAYS[(when.weekday() + 1) % 7]
	start = _minutes(from_time)

	window = None
	for candidate in frappe.get_all(
		"MS Office Hour",
		filters={"staff_user": staff, "day": weekday, "is_active": 1},
		fields=[
			"name", "from_time", "to_time", "slot_minutes", "location",
			"allow_students", "allow_guardians",
		],
		limit_page_length=0,
	):
		if _minutes(candidate.from_time) <= start < _minutes(candidate.to_time):
			window = candidate
			break
	if not window:
		return fail(
			message_en="That time is not an office hour.",
			message_ar="هذا الوقت خارج الساعات المكتبية.",
		)
	if persona == ROLE_STUDENT and not cint(window.allow_students):
		return fail(
			message_en="This window is not open to students.",
			message_ar="هذه الفترة غير متاحة لحجز الطلاب.",
		)
	if persona == ROLE_PARENT and not cint(window.allow_guardians):
		return fail(
			message_en="This window is not open to guardians.",
			message_ar="هذه الفترة غير متاحة لحجز أولياء الأمور.",
		)

	step = cint(window.slot_minutes) or 15
	end = start + step
	if end > _minutes(window.to_time):
		return fail(message_en="That slot does not fit.", message_ar="الموعد خارج الفترة.")
	if get_datetime(f"{when} {_clock(start)}:00") <= get_datetime(now()):
		return fail(message_en="That time has passed.", message_ar="هذا الوقت قد مضى.")

	# Still free? Checked against the live table, not against what the screen
	# was showing when the family pressed the button.
	for row in frappe.get_all(
		"MS Appointment",
		filters={
			"staff_user": staff,
			"appointment_date": when,
			"status": ["in", ["Requested", "Confirmed"]],
		},
		fields=["from_time", "to_time"],
		limit_page_length=0,
	):
		if _overlaps(start, end, _minutes(row.from_time), _minutes(row.to_time)):
			return fail(
				message_en="Someone booked that slot first.",
				message_ar="سبقك أحدهم إلى هذا الموعد — اختر وقتاً آخر.",
			)

	for lesson in _teaching_grid(staff):
		if lesson["day"] == weekday and _overlaps(
			start, end, _minutes(lesson["from_time"]), _minutes(lesson["to_time"])
		):
			return fail(
				message_en="The teacher is in a lesson then.",
				message_ar="المعلّم لديه حصة في هذا الوقت.",
			)

	# One open request per person per member of staff: a queue of nine from
	# the same parent is a phone call, not nine meetings.
	open_count = frappe.db.count(
		"MS Appointment",
		{
			"staff_user": staff,
			"requested_by": frappe.session.user,
			"status": ["in", ["Requested", "Confirmed"]],
		},
	)
	if open_count >= 3:
		return fail(
			message_en="You already have three open appointments with this person.",
			message_ar="لديك ٣ مواعيد مفتوحة مع هذا الموظف — أنهِ أحدها أولاً.",
		)

	student = data.get("student")
	if student:
		scope = resolve_scope(persona)
		mine = scope.get("students")
		if mine is not None and student not in mine:
			frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)

	doc = frappe.get_doc(
		{
			"doctype": "MS Appointment",
			"staff_user": staff,
			"staff_name": frappe.db.get_value("User", staff, "full_name"),
			"status": "Requested",
			"appointment_date": when,
			"from_time": _clock(start),
			"to_time": _clock(end),
			"requested_by": frappe.session.user,
			"requester_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
			"requester_role": persona,
			"student": student,
			"student_name": (
				frappe.db.get_value("Student", student, "student_name") if student else None
			),
			"student_group": data.get("student_group"),
			"subject": subject[:140],
			"notes": data.get("notes"),
			"location": window.location,
			"office_hour": window.name,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name},
		"message_en": "Appointment requested.",
		"message_ar": f"تم طلب الموعد يوم {DAY_AR[weekday]} {when} الساعة {_clock(start)}.",
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def list_appointments(
	scope: str = "all", status: str = None, upcoming: int = 0, persona: str = None
):
	"""The caller's appointments — the ones they booked and the ones booked with them."""
	user = frappe.session.user
	filters: dict = {}
	apply_period(filters, "MS Appointment")

	if scope == "incoming":
		filters["staff_user"] = user
	elif scope == "mine":
		filters["requested_by"] = user
	if status:
		filters["status"] = status
	if cint(upcoming):
		filters["appointment_date"] = [">=", nowdate()]

	if scope not in ("incoming", "mine"):
		# Both sides in one list, which needs an OR rather than a filter.
		names = set(
			frappe.get_all("MS Appointment", filters={**filters, "staff_user": user}, pluck="name")
		) | set(
			frappe.get_all(
				"MS Appointment", filters={**filters, "requested_by": user}, pluck="name"
			)
		)
		if not names:
			return {"appointments": [], "counts": _counts(user)}
		filters = {"name": ["in", list(names)]}

	rows = frappe.get_all(
		"MS Appointment",
		filters=filters,
		pluck="name",
		order_by="appointment_date desc, from_time desc",
		limit_page_length=200,
	)
	return {
		"appointments": [_row(frappe.get_doc("MS Appointment", n), user) for n in rows],
		"counts": _counts(user),
	}


def _counts(user: str) -> dict:
	return {
		"awaiting_me": frappe.db.count(
			"MS Appointment", {"staff_user": user, "status": "Requested"}
		),
		"upcoming": frappe.db.count(
			"MS Appointment",
			{
				"staff_user": user,
				"status": "Confirmed",
				"appointment_date": [">=", nowdate()],
			},
		),
		"mine_open": frappe.db.count(
			"MS Appointment",
			{"requested_by": user, "status": ["in", ["Requested", "Confirmed"]]},
		),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint(*ALL_ROLES)
def set_status(
	appointment: str = None, status: str = None, reason: str = None,
	location: str = None, persona: str = None,
):
	"""Confirm, decline, cancel or close an appointment.

	Who may do what is not symmetrical: the member of staff answers a request,
	the person who asked may withdraw it, and nobody else touches either.
	"""
	if not appointment or status not in STATUS_AR:
		return fail(message_en="Unknown status.", message_ar="حالة غير معروفة.")

	doc = frappe.get_doc("MS Appointment", appointment)
	user = frappe.session.user
	is_staff = doc.staff_user == user
	is_requester = doc.requested_by == user

	if not (is_staff or is_requester or persona in BACK_OFFICE):
		frappe.throw(_("This appointment is not yours."), frappe.PermissionError)

	if status in ("Confirmed", "Declined", "Completed"):
		if not (is_staff or persona in BACK_OFFICE):
			frappe.throw(
				_("Only the person being met can answer a request."), frappe.PermissionError
			)
	if status == "Cancelled" and not (is_requester or is_staff or persona in BACK_OFFICE):
		frappe.throw(_("This appointment is not yours."), frappe.PermissionError)

	if doc.status in ("Declined", "Cancelled", "Completed"):
		return fail(
			message_en="This appointment is already closed.",
			message_ar="هذا الموعد مغلق بالفعل.",
		)
	if status == "Declined" and not (reason or "").strip():
		return fail(
			message_en="Give a reason when declining.",
			message_ar="يرجى ذكر سبب الاعتذار.",
		)

	doc.status = status
	doc.decided_by = user
	doc.decided_on = now()
	if reason:
		doc.decline_reason = reason[:500]
	if location and is_staff:
		doc.location = location[:140]
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"data": {"id": doc.name, "status": status},
		"message_en": f"Appointment {status.lower()}.",
		"message_ar": f"تم تحديث الموعد: {STATUS_AR[status]}.",
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def my_students(persona: str = None):
	"""The children a family may book about, for the booking form."""
	scope = resolve_scope(persona)
	students = scope.get("students")
	if not students:
		return {"students": []}
	rows = frappe.get_all(
		"Student",
		filters={"name": ["in", students]},
		fields=["name", "student_name"],
		limit_page_length=0,
	)
	groups = {
		r.student: r.parent
		for r in frappe.get_all(
			"Student Group Student",
			filters={"student": ["in", students], "active": 1},
			fields=["student", "parent"],
		)
	}
	return {
		"students": [
			{"id": r.name, "name": r.student_name, "student_group": groups.get(r.name)}
			for r in rows
		]
	}
