# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WhatsAppPushSubscription(Document):
	"""One browser/device registered for WhatsApp push notifications (see butler_whatsapp/push.py)."""
