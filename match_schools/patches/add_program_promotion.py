"""Give grades an order, and record why a student was held back.

Promotion needs two things the system did not have.

**An order.** A Program is just a name; nothing said the first grade is
followed by the second. Without that there is no "next grade" to promote into,
and an administrator would have to pick the destination by hand for every
class — which is where mistakes get made at scale.

`ms_level` is the rung: 1, 2, 3 … The next grade is the one at level+1. Levels
are seeded from the Arabic ordinal in the name where it can be read, and left
empty where it cannot — a school with "توجيهي علمي" alongside "الصف الأول"
decides for itself where that sits, and guessing would be worse than asking.

**A record.** `ms_promotion_status` and `ms_promotion_notes` on Program
Enrollment say whether the student moved up, repeated, or left, and why. A
promotion that leaves no trace cannot be questioned a year later, and "why is
this child still in grade four" is a question schools do get asked.
"""

import re

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Arabic ordinals as they appear in grade names. Enough to seed the common
# case; anything unrecognised is left for the school to set.
ORDINALS = {
	"الأول": 1, "الاول": 1,
	"الثاني": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5,
	"السادس": 6, "السابع": 7, "الثامن": 8, "التاسع": 9,
	"العاشر": 10,
	"الحادي عشر": 11, "الثاني عشر": 12,
}


def execute():
	create_custom_fields(
		{
			"Program": [
				{
					"fieldname": "ms_level",
					"label": "Grade level",
					"fieldtype": "Int",
					"insert_after": "program_name",
					"description": (
						"The rung this grade sits on: 1, 2, 3 … Promotion moves a "
						"student to the grade one level higher. Leave at 0 for a "
						"programme that is not part of the ladder."
					),
				},
			],
			"Program Enrollment": [
				{
					"fieldname": "ms_promotion_status",
					"label": "Promotion outcome",
					"fieldtype": "Select",
					"options": "\n".join(["", "Promoted", "Repeated", "Graduated", "Left"]),
					"insert_after": "academic_term",
					"description": "How the student left the previous year.",
				},
				{
					"fieldname": "ms_promoted_from",
					"label": "Promoted from",
					"fieldtype": "Link",
					"options": "Program Enrollment",
					"insert_after": "ms_promotion_status",
					"read_only": 1,
				},
				{
					"fieldname": "ms_promotion_notes",
					"label": "Promotion notes",
					"fieldtype": "Small Text",
					"insert_after": "ms_promoted_from",
					"description": "Why the student was held back, or any exception granted.",
				},
			],
		},
		ignore_validate=True,
	)

	# Seed levels where the name reads as an ordinal, and only where nobody has
	# set one — a school that has already ordered its grades keeps its answer.
	seeded = 0
	for row in frappe.get_all("Program", fields=["name", "program_name"], limit_page_length=0):
		current = frappe.db.get_value("Program", row.name, "ms_level")
		if current:
			continue
		label = (row.program_name or row.name or "").strip()
		# Longest first: "الثاني عشر" must win over "الثاني".
		for word in sorted(ORDINALS, key=len, reverse=True):
			if re.search(r"(^|\s)" + re.escape(word) + r"($|\s)", label):
				frappe.db.set_value(
					"Program", row.name, "ms_level", ORDINALS[word], update_modified=False
				)
				seeded += 1
				break

	frappe.db.commit()
	print(f"Seeded a level for {seeded} programme(s).")
