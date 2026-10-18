# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WhatsAppNumberReview(Document):
	def validate(self):
		from butler_whatsapp.numbers import validate_review

		validate_review(self)
