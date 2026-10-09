app_name = "butler_whatsapp"
app_title = "WhatsApp"
app_publisher = "Butler"
app_description = "WhatsApp inbox, document sending and push notifications for ERPNext (Meta Cloud API)"
app_email = "alharam20023@gmail.com"
app_license = "mit"

required_apps = ["erpnext", "frappe_whatsapp"]

add_to_apps_screen = [
	{
		"name": "butler_whatsapp",
		"title": "واتساب",
		"route": "/desk/whatsapp-inbox",
		"logo": "/assets/butler_whatsapp/images/whatsapp-app.svg",
		"has_permission": "butler_whatsapp.permissions.has_app_permission",
	}
]

after_install = "butler_whatsapp.install.after_install"
after_migrate = "butler_whatsapp.install.after_migrate"

# Floating launcher + unread badge, "Send via WhatsApp" on sales documents and Web Push.
# A bundle so every deploy gets a new hashed URL (no stale browser cache).
app_include_js = ["butler_whatsapp.bundle.js"]

doc_events = {
	"WhatsApp Message": {
		# live inbox updates + phone/desktop notifications (realtime.py, push.py)
		"after_insert": "butler_whatsapp.realtime.on_message_insert",
		# incoming media -> private file; delivery ticks in the inbox (inbox.py)
		"on_update": "butler_whatsapp.inbox.on_message_update",
	},
}

# Supervisors see every conversation, everybody else only the ones assigned to them (access.py)
permission_query_conditions = {
	"WhatsApp Message": "butler_whatsapp.access.message_query",
	"WhatsApp Contact": "butler_whatsapp.access.contact_query",
}
has_permission = {
	"WhatsApp Message": "butler_whatsapp.access.message_has_permission",
	"WhatsApp Contact": "butler_whatsapp.access.contact_has_permission",
}

# launcher visibility + supervisor flag for the Desk bundle
boot_session = "butler_whatsapp.access.boot_session"

# "WhatsApp" button on Customer / Lead / Contact is added by the bundle (all Desk pages)
