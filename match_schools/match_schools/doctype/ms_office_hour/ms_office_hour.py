"""One weekly window in which a member of staff will see people.

Stored per weekday rather than per date: a teacher declares "Sunday 10:00 to
11:00" once, and the booking screen projects it forward. Individual dates are
excluded by the appointments already booked against them, not by editing this.
"""

from frappe.model.document import Document


class MSOfficeHour(Document):
	pass
