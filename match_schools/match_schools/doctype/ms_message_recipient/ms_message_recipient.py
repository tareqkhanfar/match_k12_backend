"""One person's copy of a message.

A mailbox is per person, not per message: the same message is unread for one
recipient and archived by another. Keeping read, archived, starred and deleted
here rather than on the message is what makes that possible — and what lets a
message be addressed to twenty people without twenty copies of the body.
"""

from frappe.model.document import Document


class MSMessageRecipient(Document):
	pass
