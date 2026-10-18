# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WhatsAppCampaign(Document):
	def validate(self):
		from butler_whatsapp.campaigns import validate_campaign

		validate_campaign(self)
