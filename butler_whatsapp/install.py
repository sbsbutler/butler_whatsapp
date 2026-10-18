"""Setup for the WhatsApp app: role, custom fields, permissions, page access.

Runs after install and after every migrate, so it must stay idempotent.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Sales staff who answer customers in the WhatsApp inbox
WA_AGENT = "WhatsApp Agent"
WA_PERMS = {
	"WhatsApp Message": ("read", "write", "create"),
	"WhatsApp Contact": ("read", "write", "create"),
}
WA_PAGE = "whatsapp-inbox"


def after_install():
	ensure_setup()


def after_migrate():
	ensure_setup()


def ensure_setup():
	if not frappe.db.exists("Role", WA_AGENT):
		frappe.get_doc({"doctype": "Role", "role_name": WA_AGENT, "desk_access": 1}).insert(ignore_permissions=True)
	ensure_whatsapp_inbox()


def ensure_whatsapp_inbox():
	"""Custom fields, role permissions and page access for the WhatsApp inbox."""
	if not frappe.db.exists("DocType", "WhatsApp Message"):
		return  # frappe_whatsapp not installed

	first_time = not frappe.db.has_column("WhatsApp Message", "butler_read_at")
	fields = {
		"WhatsApp Message": [
			{
				"fieldname": "butler_read_by",
				"fieldtype": "Link",
				"options": "User",
				"label": "Read By",
				"insert_after": "profile_name",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "butler_read_at",
				"fieldtype": "Datetime",
				"label": "Read At",
				"insert_after": "butler_read_by",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "butler_error",
				"fieldtype": "Small Text",
				"label": "Delivery Failure Reason",
				"insert_after": "butler_read_at",
				"read_only": 1,
				"no_copy": 1,
			},
		]
	}
	if frappe.db.exists("DocType", "WhatsApp Templates"):
		fields["WhatsApp Templates"] = [
			{
				"fieldname": "butler_enabled",
				"fieldtype": "Check",
				"label": "Enabled in the Butler Inbox",
				"default": "1",
				"insert_after": "status",
				"in_list_view": 1,
				"in_standard_filter": 1,
				"description": "Switch off to hide the template and stop the system from sending it (it is not deleted at Meta).",
			},
		]
	if frappe.db.exists("DocType", "WhatsApp Contact"):
		fields["WhatsApp Contact"] = [
			{
				"fieldname": "butler_customer",
				"fieldtype": "Link",
				"options": "Customer",
				"label": "Customer",
				"insert_after": "email",
			},
			{
				"fieldname": "butler_lead",
				"fieldtype": "Link",
				"options": "Lead",
				"label": "Lead",
				"insert_after": "butler_customer",
			},
			{
				"fieldname": "butler_assigned_to",
				"fieldtype": "Link",
				"options": "User",
				"label": "Assigned To",
				"insert_after": "butler_lead",
				"search_index": 1,
			},
			{
				"fieldname": "butler_team",
				"fieldtype": "Link",
				"options": "WhatsApp Team",
				"label": "Team",
				"insert_after": "butler_assigned_to",
				"search_index": 1,
				"in_standard_filter": 1,
			},
			{
				"fieldname": "butler_auto_assigned",
				"fieldtype": "Check",
				"label": "Assigned automatically",
				"insert_after": "butler_team",
				"read_only": 1,
				"hidden": 1,
			},
			{
				"fieldname": "butler_status",
				"fieldtype": "Select",
				"options": "Open\nClosed",
				"default": "Open",
				"label": "Conversation Status",
				"insert_after": "butler_auto_assigned",
			},
			{
				"fieldname": "butler_no_auto_link",
				"fieldtype": "Check",
				"label": "Do not auto-link by phone",
				"insert_after": "butler_status",
			},
			{
				"fieldname": "butler_consent",
				"fieldtype": "Select",
				"options": "\nOpted In\nOpted Out",
				"label": "WhatsApp Consent",
				"insert_after": "butler_no_auto_link",
				"in_list_view": 1,
				"in_standard_filter": 1,
			},
			{
				"fieldname": "butler_consent_source",
				"fieldtype": "Data",
				"label": "Consent Source",
				"insert_after": "butler_consent",
				"read_only": 1,
			},
			{
				"fieldname": "butler_consent_at",
				"fieldtype": "Datetime",
				"label": "Consent / Opt-out Date",
				"insert_after": "butler_consent_source",
				"read_only": 1,
			},
			{
				"fieldname": "butler_consent_by",
				"fieldtype": "Link",
				"options": "User",
				"label": "Recorded By",
				"insert_after": "butler_consent_at",
				"read_only": 1,
			},
			{
				"fieldname": "butler_last_auto_reply",
				"fieldtype": "Datetime",
				"label": "Last automatic reply",
				"insert_after": "butler_consent_by",
				"read_only": 1,
				"hidden": 1,
			},
			{
				"fieldname": "butler_sla_alerted",
				"fieldtype": "Data",
				"label": "SLA alert sent for",
				"insert_after": "butler_last_auto_reply",
				"read_only": 1,
				"hidden": 1,
			},
		]
	if frappe.db.exists("DocType", "Appointment"):
		fields["Appointment"] = [
			{
				"fieldname": "butler_wa_section",
				"fieldtype": "Section Break",
				"label": "WhatsApp",
				"insert_after": "verification_token",
				"collapsible": 1,
			},
			{
				"fieldname": "butler_wa_consent",
				"fieldtype": "Check",
				"label": "Customer agreed to receive WhatsApp messages",
				"insert_after": "butler_wa_section",
				"description": "Records the customer's general consent to WhatsApp messages (Meta policy). The appointment confirmation and reminder do not need it.",
			},
			{
				"fieldname": "butler_wa_confirmation_sent",
				"fieldtype": "Datetime",
				"label": "WhatsApp Confirmation Sent",
				"insert_after": "butler_wa_consent",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "butler_wa_col",
				"fieldtype": "Column Break",
				"insert_after": "butler_wa_confirmation_sent",
			},
			{
				"fieldname": "butler_wa_reminder_sent",
				"fieldtype": "Datetime",
				"label": "WhatsApp Reminder Sent",
				"insert_after": "butler_wa_col",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "butler_wa_error",
				"fieldtype": "Small Text",
				"label": "WhatsApp Error",
				"insert_after": "butler_wa_reminder_sent",
				"read_only": 1,
				"no_copy": 1,
				"depends_on": "eval:doc.butler_wa_error",
			},
		]
	create_custom_fields(fields, ignore_validate=True)
	_migrate_legacy_values()

	if first_time:
		# History before the inbox existed counts as read (no flood of "unread").
		frappe.db.sql(
			"""UPDATE `tabWhatsApp Message` SET butler_read_at = creation
			WHERE type = 'Incoming' AND butler_read_at IS NULL"""
		)

	if frappe.db.has_column("WhatsApp Templates", "butler_enabled"):
		# templates that existed before the switch are on
		frappe.db.sql("UPDATE `tabWhatsApp Templates` SET butler_enabled = 1 WHERE butler_enabled IS NULL")

	_ensure_agent_permissions()
	_ensure_page_role()
	_ensure_report_role()

	from butler_whatsapp.butler_whatsapp.doctype.whatsapp_template_draft.whatsapp_template_draft import (
		seed_drafts,
	)
	from butler_whatsapp.teams import clear_cache, seed_teams

	seed_drafts()
	seed_teams()
	clear_cache()

	from butler_whatsapp.inbox import make_existing_attachments_private, repair_attachment_urls
	from butler_whatsapp.realtime import remove_existing_duplicates

	make_existing_attachments_private()
	remove_existing_duplicates()
	repair_attachment_urls()


def _ensure_agent_permissions():
	from frappe.permissions import add_permission, update_permission_property

	for doctype, ptypes in WA_PERMS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": WA_AGENT, "permlevel": 0}):
			add_permission(doctype, WA_AGENT, 0)
		for ptype in ptypes:
			update_permission_property(doctype, WA_AGENT, 0, ptype, 1, validate=False)
		frappe.clear_cache(doctype=doctype)


def _ensure_page_role():
	if not frappe.db.exists("Page", WA_PAGE):
		return
	page = frappe.get_doc("Page", WA_PAGE)
	have = {r.role for r in page.roles}
	missing = [r for r in ("System Manager", WA_AGENT) if r not in have]
	if missing:
		for role in missing:
			page.append("roles", {"role": role})
		page.flags.ignore_permissions = True
		page.db_update_all()  # avoid Page.validate (blocks edits outside developer mode)
		frappe.clear_cache()


def _ensure_report_role():
	"""Performance report: supervisors are System Managers; agents see only their own row."""
	name = "WhatsApp Inbox Performance"
	if not frappe.db.exists("Report", name):
		return
	report = frappe.get_doc("Report", name)
	have = {r.role for r in report.roles}
	missing = [r for r in ("System Manager", WA_AGENT) if r not in have]
	if missing:
		for role in missing:
			report.append("roles", {"role": role})
		report.flags.ignore_permissions = True
		report.db_update_all()


def _migrate_legacy_values():
	"""Stored values were Arabic before the app was translated; switch them to the
	English values the code uses now (the UI shows them translated)."""
	from butler_whatsapp.butler_whatsapp.doctype.whatsapp_template_draft.whatsapp_template_draft import (
		LEGACY_STATUS,
		LEGACY_USAGE,
	)
	from butler_whatsapp.compliance import LEGACY_VALUES

	def update(table, column, mapping):
		if not frappe.db.has_column(table, column):
			return
		for old, new in mapping.items():
			frappe.db.sql(f"UPDATE `tab{table}` SET `{column}` = %s WHERE `{column}` = %s", (new, old))

	update("WhatsApp Contact", "butler_consent", LEGACY_VALUES["consent"])
	update("WhatsApp Contact", "butler_consent_source", LEGACY_VALUES["source"])
	update("WhatsApp Template Draft", "draft_status", LEGACY_STATUS)
	update("WhatsApp Template Draft", "usage", LEGACY_USAGE)
	singles = {"webhook_signature_mode": LEGACY_VALUES["signature"], "auto_assign": {"بالتناوب": "Round Robin"}}
	for field, mapping in singles.items():
		for old, new in mapping.items():
			frappe.db.sql(
				"UPDATE `tabSingles` SET value = %s WHERE doctype = 'WhatsApp Inbox Settings' AND field = %s AND value = %s",
				(new, field, old),
			)
	frappe.clear_document_cache("WhatsApp Inbox Settings", "WhatsApp Inbox Settings")
	_fill_new_setting_defaults()


def _fill_new_setting_defaults():
	"""New fields of an already saved Single get no value (defaults only apply to
	new documents): store the default of every field that has never been saved."""
	settings = "WhatsApp Inbox Settings"
	if not frappe.db.exists("DocType", settings):
		return
	saved = set(frappe.db.sql_list("SELECT field FROM `tabSingles` WHERE doctype = %s", (settings,)))
	if not saved:
		return  # never saved: the form shows the defaults
	for df in frappe.get_meta(settings).fields:
		if df.fieldname in saved or df.default in (None, "") or df.fieldtype in ("Section Break", "Column Break", "HTML"):
			continue
		frappe.db.set_single_value(settings, df.fieldname, df.default)
	frappe.clear_document_cache(settings, settings)
