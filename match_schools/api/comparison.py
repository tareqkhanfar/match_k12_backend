"""Where one pupil stands against the class they sit in.

A percentage on its own answers nothing a family actually asks. 72% is a good
mark in a subject where the class averages 60 and a worrying one where it
averages 88, and the number alone cannot tell you which. So every figure here
comes with the class beside it, and with a rank that says how many pupils sat
the same paper.

Two views. One subject in detail — every assessment, the pupil's mark, the
class average, the gap — and all subjects at once, which is the view that
shows a pupil who is fine everywhere except one place.

Comparison is always within the sections the pupil actually belongs to. "Above
average" against the whole school is not a fact about this pupil; it is a fact
about which section they were put in.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from match_schools.api.utils import (
	ROLE_ADMIN,
	ROLE_PARENT,
	ROLE_SECRETARY,
	ROLE_STUDENT,
	ROLE_TEACHER,
	fail,
	get_default_academic_term,
	get_default_academic_year,
	ms_endpoint,
	resolve_scope,
)

BACK_OFFICE = (ROLE_ADMIN, ROLE_SECRETARY)
STAFF = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_TEACHER)
ALL_ROLES = (*STAFF, ROLE_STUDENT, ROLE_PARENT)

# Below this many marks an "average" is one or two children, and a rank is
# noise. The screens say so rather than drawing a chart of nothing.
MIN_SAMPLE = 3


def _assert_may_see(persona: str, student: str) -> None:
	if persona in BACK_OFFICE:
		return
	if persona == ROLE_TEACHER:
		scope = resolve_scope(persona)
		groups = set(scope.get("student_groups") or [])
		instructor = scope.get("instructor")
		if instructor:
			groups |= {
				r.student_group
				for r in frappe.get_all(
					"MS Timetable Slot",
					filters={"instructor": instructor, "active": 1},
					fields=["student_group"],
					limit_page_length=0,
				)
				if r.student_group
			}
		mine = frappe.get_all(
			"Student Group Student",
			filters={"student": student, "active": 1},
			pluck="parent",
		)
		if not (groups & set(mine)):
			frappe.throw(_("This student is not in your classes."), frappe.PermissionError)
		return

	if student not in (resolve_scope(persona).get("students") or []):
		frappe.throw(_("You are not allowed to view this student."), frappe.PermissionError)


def _groups_of(student: str) -> list[str]:
	return frappe.get_all(
		"Student Group Student",
		filters={"student": student, "active": 1},
		pluck="parent",
	)


def _band(gap: float | None) -> str:
	"""Plain language for a distance from the average.

	Bands rather than a raw number, because "أعلى بـ 3.4 نقطة" invites a
	family to read precision into a figure that moves with every mark.
	"""
	if gap is None:
		return "unknown"
	if gap >= 10:
		return "well_above"
	if gap >= 3:
		return "above"
	if gap > -3:
		return "around"
	if gap > -10:
		return "below"
	return "well_below"


BAND_AR = {
	"well_above": "أعلى من المعدل بوضوح",
	"above": "أعلى من المعدل",
	"around": "قريب من المعدل",
	"below": "أقل من المعدل",
	"well_below": "أقل من المعدل بوضوح",
	"unknown": "لا توجد مقارنة كافية",
}
BAND_TONE = {
	"well_above": "success",
	"above": "success",
	"around": "info",
	"below": "warning",
	"well_below": "danger",
	"unknown": "muted",
}


def _percentages(rows: list) -> dict[str, list[float]]:
	"""Marks grouped by pupil, as percentages of what each was out of."""
	out: dict[str, list[float]] = {}
	for r in rows:
		maximum = flt(r.max_score)
		if maximum <= 0:
			continue
		out.setdefault(r.student, []).append(flt(r.score) / maximum * 100)
	return out


def _rank(value: float, others: list[float]) -> dict:
	"""Where a figure sits among the class's figures.

	Ranked by how many did better, so equal marks share a place rather than
	being ordered by an accident of who was entered first.
	"""
	better = sum(1 for v in others if v > value)
	return {
		"rank": better + 1,
		"of": len(others),
		"percentile": round((1 - better / len(others)) * 100) if others else None,
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def subject_comparison(
	student: str = None,
	course: str = None,
	academic_term: str = None,
	persona: str = None,
):
	"""One subject, assessment by assessment, against the class."""
	if not (student and course):
		return fail(
			message_en="A student and a subject are required.",
			message_ar="يجب تحديد الطالب والمادة.",
		)
	_assert_may_see(persona, student)

	groups = _groups_of(student)
	if not groups:
		return {"assessments": [], "summary": None}

	term = academic_term or get_default_academic_term()
	filters = {"course": course, "student_group": ["in", groups]}
	if term:
		filters["academic_term"] = term

	rows = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["student", "component_name", "score", "max_score", "student_group"],
		limit_page_length=0,
	)
	if not rows:
		return {"assessments": [], "summary": None, "course": course}

	# Per assessment: the pupil's mark, and everyone else's on the same paper.
	by_component: dict[str, dict[str, list[float]]] = {}
	for r in rows:
		maximum = flt(r.max_score)
		if maximum <= 0:
			continue
		by_component.setdefault(r.component_name, {}).setdefault(r.student, []).append(
			flt(r.score) / maximum * 100
		)

	assessments = []
	for component, per_student in by_component.items():
		values = {s: sum(v) / len(v) for s, v in per_student.items()}
		mine = values.get(student)
		if mine is None:
			continue
		others = list(values.values())
		average = sum(others) / len(others)
		gap = mine - average
		enough = len(others) >= MIN_SAMPLE
		assessments.append(
			{
				"component": component,
				"student_percent": round(mine, 1),
				"class_average": round(average, 1) if enough else None,
				"class_high": round(max(others), 1) if enough else None,
				"class_low": round(min(others), 1) if enough else None,
				"gap": round(gap, 1) if enough else None,
				"band": _band(gap) if enough else "unknown",
				"band_label": BAND_AR[_band(gap) if enough else "unknown"],
				"tone": BAND_TONE[_band(gap) if enough else "unknown"],
				"sample": len(others),
				**(_rank(mine, others) if enough else {"rank": None, "of": len(others), "percentile": None}),
			}
		)

	assessments.sort(key=lambda a: a["component"])

	# The subject as a whole: each pupil's own average across everything they
	# sat, so a pupil who missed one paper is not compared on a total they
	# never had the chance to earn.
	per_student = _percentages(rows)
	means = {s: sum(v) / len(v) for s, v in per_student.items() if v}
	mine = means.get(student)
	summary = None
	if mine is not None:
		others = list(means.values())
		average = sum(others) / len(others)
		gap = mine - average
		enough = len(others) >= MIN_SAMPLE
		summary = {
			"student_percent": round(mine, 1),
			"class_average": round(average, 1) if enough else None,
			"class_high": round(max(others), 1) if enough else None,
			"class_low": round(min(others), 1) if enough else None,
			"gap": round(gap, 1) if enough else None,
			"band": _band(gap) if enough else "unknown",
			"band_label": BAND_AR[_band(gap) if enough else "unknown"],
			"tone": BAND_TONE[_band(gap) if enough else "unknown"],
			"sample": len(others),
			**(_rank(mine, others) if enough else {"rank": None, "of": len(others), "percentile": None}),
		}

	return {
		"student": student,
		"course": course,
		"course_name": frappe.db.get_value("Course", course, "course_name") or course,
		"academic_term": term,
		"assessments": assessments,
		"summary": summary,
		"min_sample": MIN_SAMPLE,
	}


@frappe.whitelist()
@ms_endpoint(*ALL_ROLES)
def overall_comparison(student: str = None, academic_term: str = None, persona: str = None):
	"""Every subject at once — the view that finds the one weak place."""
	if not student:
		return fail(message_en="A student is required.", message_ar="يجب تحديد الطالب.")
	_assert_may_see(persona, student)

	groups = _groups_of(student)
	if not groups:
		return {"subjects": [], "overall": None}

	term = academic_term or get_default_academic_term()
	filters = {"student_group": ["in", groups]}
	if term:
		filters["academic_term"] = term

	rows = frappe.get_all(
		"MS Gradebook Entry",
		filters=filters,
		fields=["student", "course", "component_name", "score", "max_score"],
		limit_page_length=0,
	)
	if not rows:
		return {"subjects": [], "overall": None}

	by_course: dict[str, dict[str, list[float]]] = {}
	for r in rows:
		maximum = flt(r.max_score)
		if maximum <= 0 or not r.course:
			continue
		by_course.setdefault(r.course, {}).setdefault(r.student, []).append(
			flt(r.score) / maximum * 100
		)

	names = {
		r.name: r.course_name
		for r in frappe.get_all(
			"Course",
			filters={"name": ["in", list(by_course)] or [""]},
			fields=["name", "course_name"],
		)
	}

	subjects = []
	for course, per_student in by_course.items():
		means = {s: sum(v) / len(v) for s, v in per_student.items() if v}
		mine = means.get(student)
		if mine is None:
			continue
		others = list(means.values())
		average = sum(others) / len(others)
		gap = mine - average
		enough = len(others) >= MIN_SAMPLE
		subjects.append(
			{
				"course": course,
				"course_name": names.get(course) or course,
				"student_percent": round(mine, 1),
				"class_average": round(average, 1) if enough else None,
				"class_high": round(max(others), 1) if enough else None,
				"gap": round(gap, 1) if enough else None,
				"band": _band(gap) if enough else "unknown",
				"band_label": BAND_AR[_band(gap) if enough else "unknown"],
				"tone": BAND_TONE[_band(gap) if enough else "unknown"],
				"sample": len(others),
				"assessments": len(per_student.get(student, [])),
				**(_rank(mine, others) if enough else {"rank": None, "of": len(others), "percentile": None}),
			}
		)

	# Weakest first: the point of this screen is to find where to help.
	subjects.sort(key=lambda s: (s["gap"] if s["gap"] is not None else 999))

	overall = None
	if subjects:
		compared = [s for s in subjects if s["gap"] is not None]
		mine = sum(s["student_percent"] for s in subjects) / len(subjects)
		overall = {
			"student_percent": round(mine, 1),
			"class_average": (
				round(sum(s["class_average"] for s in compared) / len(compared), 1)
				if compared
				else None
			),
			"subjects": len(subjects),
			"above": sum(1 for s in compared if s["gap"] > 0),
			"below": sum(1 for s in compared if s["gap"] < 0),
			"strongest": max(compared, key=lambda s: s["gap"])["course_name"] if compared else None,
			"weakest": min(compared, key=lambda s: s["gap"])["course_name"] if compared else None,
		}

	return {
		"student": student,
		"student_name": frappe.db.get_value("Student", student, "student_name"),
		"academic_term": term,
		"academic_year": get_default_academic_year(),
		"subjects": subjects,
		"overall": overall,
		"min_sample": MIN_SAMPLE,
	}
