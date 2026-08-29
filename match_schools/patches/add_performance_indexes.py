"""Index the columns every screen filters by.

Frappe indexes `name`, `creation` and `modified`, and nothing else unless a
field is marked searchable. Every hot query in this app filters by something
else — marks by class and subject, the timetable by class and date, alerts by
student — so each one was a full table scan.

At a few hundred rows that is invisible. A real school writes tens of
thousands of marks a year and a timetable row per lesson per week, and the
same query that returns in 8ms today takes seconds once the table is large.
The cost of being wrong here is a system that works in the pilot and collapses
in the second term.

Composite where the query uses both columns together, single where it does
not. Indexes are added only if missing, so this is safe to re-run.
"""

import frappe

INDEXES = {
	"MS Gradebook Entry": [
		("ms_gbe_group_course", ["student_group", "course"]),
		("ms_gbe_student", ["student"]),
		("ms_gbe_term", ["academic_term"]),
	],
	"MS Student Alert": [
		("ms_alert_student_status", ["student", "status"]),
	],
	"MS Term Submission": [
		("ms_sub_group_course", ["student_group", "course"]),
	],
	"MS Lesson Plan": [
		("ms_lp_group", ["student_group"]),
	],
	"MS Gallery Album": [
		("ms_alb_group", ["student_group"]),
	],
	"Course Schedule": [
		("ms_cs_group_date", ["student_group", "schedule_date"]),
		("ms_cs_instructor", ["instructor"]),
	],
	"MS Assignment Submission": [
		("ms_asub_student", ["student"]),
	],
}


def execute():
	added = 0
	for doctype, indexes in INDEXES.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		table = f"tab{doctype}"
		existing = {
			r.get("Key_name") for r in frappe.db.sql(f"show index from `{table}`", as_dict=True)
		}
		columns = {
			r.get("Field") for r in frappe.db.sql(f"show columns from `{table}`", as_dict=True)
		}
		for name, cols in indexes:
			if name in existing:
				continue
			# A column may be missing on a site that has not migrated yet;
			# indexing it would abort the whole patch.
			if any(c not in columns for c in cols):
				continue
			joined = ", ".join(f"`{c}`" for c in cols)
			try:
				frappe.db.sql(f"ALTER TABLE `{table}` ADD INDEX `{name}` ({joined})")
				added += 1
			except Exception as e:
				print(f"  skipped {table}.{name}: {e}")

	frappe.db.commit()
	print(f"Added {added} index(es).")
