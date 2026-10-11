"""Send ERPNext documents (PDF) to customers over WhatsApp.

Builds on frappe_whatsapp: credentials come from its "WhatsApp Account"
(token stored in a Password field) and every send is logged as a
"WhatsApp Message" so it shows up in the chat window and message list.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import fmt_money, formatdate, get_datetime, now_datetime

from butler_whatsapp.meta import (
	CloudAPI,
	WhatsAppError,
	count_body_variables,
	document_message,
	normalize_msisdn,
	template_document_message,
	within_service_window,
)

# Doctypes that get the "Send via WhatsApp" button (see public/js/whatsapp.js)
SUPPORTED_DOCTYPES = ("Quotation", "Butler Quotation", "Sales Order", "Sales Invoice")

# Where to look for the customer's mobile, in order
MOBILE_FIELDS = ("contact_mobile", "mobile_no", "contact_phone", "phone")

# WhatsApp Inbox Settings field holding the default template per doctype
DEFAULT_TEMPLATE_FIELD = {
	"Quotation": "quotation_template",
	"Butler Quotation": "quotation_template",
	"Sales Order": "order_template",
	"Sales Invoice": "invoice_template",
}

DEFAULT_CAPTION = {
	"Quotation": "عرض سعر رقم {name} من شركة باتلر",
	"Butler Quotation": "عرض سعر رقم {name} من شركة باتلر",
	"Sales Order": "أمر بيع رقم {name} من شركة باتلر",
	"Sales Invoice": "فاتورة رقم {name} من شركة باتلر",
}


def _check_access(doctype: str, name: str):
	if doctype not in SUPPORTED_DOCTYPES:
		frappe.throw(_("WhatsApp sending is not enabled for {0}").format(doctype))
	frappe.has_permission(doctype, "read", name, throw=True)
	frappe.has_permission("WhatsApp Message", "create", throw=True)


def _account():
	name = frappe.db.get_value("WhatsApp Account", {"is_default_outgoing": 1, "status": "Active"}, "name")
	if not name:
		frappe.throw(_("No active default outgoing WhatsApp Account"))
	return frappe.get_doc("WhatsApp Account", name)


def _client(account) -> CloudAPI:
	return CloudAPI(account.url, account.version, account.phone_id, account.get_password("token"))


def _default_mobile(doc) -> str | None:
	for field in MOBILE_FIELDS:
		if doc.get(field):
			return doc.get(field)
	customer = doc.get("customer") or (doc.get("party_name") if doc.get("quotation_to") == "Customer" else None)
	if customer:
		return frappe.db.get_value("Customer", customer, "mobile_no")
	return None


def _last_incoming(msisdn: str):
	value = frappe.db.get_value(
		"WhatsApp Message",
		{"type": "Incoming", "from": ["in", [msisdn, "+" + msisdn]]},
		"creation",
		order_by="creation desc",
	)
	return get_datetime(value) if value else None


def _print_formats(doctype: str) -> tuple[list[str], str | None]:
	formats = frappe.get_all(
		"Print Format", filters={"doc_type": doctype, "disabled": 0}, pluck="name", order_by="name asc"
	)
	default = frappe.db.get_value("Property Setter", {"doc_type": doctype, "property": "default_print_format"}, "value")
	return formats, default or (formats[0] if formats else None)


def _document_templates(account_name: str) -> list[dict]:
	from butler_whatsapp.compliance import template_is_enabled

	rows = [
		r for r in frappe.get_all(
			"WhatsApp Templates",
			filters={"status": "APPROVED", "header_type": "DOCUMENT", "whatsapp_account": ["in", [account_name, "", None]]},
			fields=["name", "actual_name", "language_code", "template", "category", "butler_enabled"],
			order_by="actual_name asc",
		)
		if template_is_enabled(r)
	]
	for row in rows:
		row["text"] = row.pop("template")
		row["variables"] = count_body_variables(row["text"])
		row.pop("butler_enabled", None)
	return rows


def _param_defaults(doc) -> list[str]:
	"""Values for {{1}}..{{4}} of our document templates: name, number, amount, due date."""
	amount = doc.get("rounded_total") or doc.get("grand_total")
	due = doc.get("due_date") or doc.get("valid_till") or doc.get("delivery_date")
	return [
		doc.get("customer_name") or doc.get("party_name") or doc.get("title") or "",
		doc.name,
		fmt_money(amount, precision=2) if amount else "",
		formatdate(due, "dd-MM-yyyy") if due else "",
	]


@frappe.whitelist()
def get_send_context(doctype: str, name: str, mobile: str | None = None) -> dict:
	"""Everything the dialog needs: default number, 24h window, print formats, templates."""
	_check_access(doctype, name)
	doc = frappe.get_doc(doctype, name)
	account = _account()
	mobile = mobile or _default_mobile(doc)
	msisdn = normalize_msisdn(mobile)
	last_in = _last_incoming(msisdn) if msisdn else None
	formats, default_format = _print_formats(doctype)
	from butler_whatsapp import access
	from butler_whatsapp.compliance import get_consent

	templates = _document_templates(account.name)
	field = DEFAULT_TEMPLATE_FIELD.get(doctype)
	default_template = access.settings().get(field) if field else None
	if default_template not in {t.name for t in templates}:
		default_template = None
	return {
		"mobile": mobile,
		"msisdn": msisdn,
		"in_window": within_service_window(last_in, now_datetime()),
		"last_incoming": str(last_in) if last_in else None,
		"print_formats": formats,
		"default_print_format": default_format,
		"caption": DEFAULT_CAPTION.get(doctype, "{name}").format(name=name),
		"templates": templates,
		"default_template": default_template,
		"param_defaults": _param_defaults(doc),
		"consent": get_consent(msisdn) if msisdn else None,
		"customer_name": doc.get("customer_name") or doc.get("title") or "",
		"grand_total": doc.get("rounded_total") or doc.get("grand_total"),
		"currency": doc.get("currency"),
	}


@frappe.whitelist(methods=["POST"])
def send_document(
	doctype: str,
	name: str,
	mobile: str,
	print_format: str | None = None,
	caption: str | None = None,
	template: str | None = None,
	body_params: str | list | None = None,
) -> dict:
	"""Render the document as PDF, upload it to Meta and send it.

	Free-form document message inside the 24h service window; otherwise an
	approved template with a DOCUMENT header is required.
	"""
	_check_access(doctype, name)
	msisdn = normalize_msisdn(mobile)
	if not msisdn:
		frappe.throw(_("Invalid mobile number: {0}").format(mobile))

	account = _account()
	in_window = within_service_window(_last_incoming(msisdn), now_datetime())
	tpl = None
	if template:
		tpl = frappe.get_doc("WhatsApp Templates", template)
		if tpl.status != "APPROVED" or tpl.header_type != "DOCUMENT":
			frappe.throw(_("Template {0} must be approved and have a DOCUMENT header").format(template))
		from butler_whatsapp.compliance import check_template_send

		check_template_send(msisdn, tpl)
	elif not in_window:
		frappe.throw(
			_("The customer has not messaged you in the last 24 hours. Choose an approved template, "
			  "or ask the customer to send any message first.")
		)

	params = frappe.parse_json(body_params) if isinstance(body_params, str) else (body_params or [])
	if tpl and len(params) != count_body_variables(tpl.template):
		frappe.throw(_("Template {0} needs {1} value(s)").format(tpl.actual_name, count_body_variables(tpl.template)))

	pdf = frappe.get_print(doctype, name, print_format=print_format, as_pdf=True)
	filename = f"{name}.pdf".replace("/", "-")
	api = _client(account)
	try:
		media_id = api.upload_media(pdf, filename)
		if tpl:
			payload = template_document_message(msisdn, tpl.actual_name, tpl.language_code, media_id, filename, params)
		else:
			payload = document_message(msisdn, media_id, filename, caption)
		message_id = api.send(payload)
	except WhatsAppError as e:
		frappe.log_error(title="WhatsApp send_document failed", message=frappe.get_traceback())
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": pdf,
			"is_private": 1,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
		}
	).insert(ignore_permissions=True)

	_log_message(account.name, msisdn, message_id, doctype, name, file_doc.file_url,
		caption or "", tpl.name if tpl else None)
	from butler_whatsapp.inbox import claim_if_unassigned, publish

	claim_if_unassigned(msisdn)
	publish(msisdn, "outgoing")
	frappe.get_doc(doctype, name).add_comment(
		"Info", _("Sent via WhatsApp to {0}").format("+" + msisdn)
	)
	return {"ok": True, "message_id": message_id, "to": msisdn, "template": tpl.name if tpl else None}


def _log_message(account, msisdn, message_id, doctype, name, file_url, caption, template):
	"""Record the already-sent message without triggering frappe_whatsapp's own send."""
	msg = frappe.new_doc("WhatsApp Message")
	msg.update(
		{
			"type": "Outgoing",
			"to": msisdn,
			"message": caption,
			"content_type": "document",
			"attach": file_url,
			"message_type": "Template" if template else "Manual",
			"template": template,
			"message_id": message_id,
			"status": "Success",
			"whatsapp_account": account,
			"reference_doctype": doctype,
			"reference_name": name,
		}
	)
	now = now_datetime()
	msg.owner = msg.modified_by = frappe.session.user
	msg.creation = msg.modified = now
	msg.set_new_name()
	msg.db_insert()  # skips before_insert (which would send a second time)
