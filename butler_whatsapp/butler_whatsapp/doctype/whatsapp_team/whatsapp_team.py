# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WhatsAppTeam(Document):
	def on_update(self):
		from butler_whatsapp.teams import on_team_update

		on_team_update(self)

	def on_trash(self):
		from butler_whatsapp.teams import clear_cache

		clear_cache()
