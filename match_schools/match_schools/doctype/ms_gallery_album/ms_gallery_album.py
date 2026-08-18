"""A class's photo album for one event."""

import frappe
from frappe.model.document import Document


class MSGalleryAlbum(Document):
	def validate(self):
		self.photo_count = len(self.photos or [])
		# The first photo stands in as the cover until someone picks one, so a
		# freshly uploaded album is never a blank card on the wall.
		if not self.cover_image and self.photos:
			self.cover_image = self.photos[0].file_url
		# A cover that was deleted from the album would 404 on every card.
		elif self.cover_image and self.photos:
			urls = {p.file_url for p in self.photos}
			if self.cover_image not in urls:
				self.cover_image = self.photos[0].file_url
		elif not self.photos:
			self.cover_image = None
