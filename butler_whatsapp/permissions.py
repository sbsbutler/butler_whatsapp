import frappe


def has_app_permission():
	"""Show the WhatsApp app to users who can read WhatsApp messages."""
	return bool(frappe.has_permission("WhatsApp Message", "read"))
