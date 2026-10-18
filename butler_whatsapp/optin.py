"""Ways to collect the customer's consent (opt-in) the way Meta asks for it.

Meta: the customer must clearly agree to receive messages, and the business
name (Butler) must be stated. Any channel is fine as long as it is recorded.

1. Subscription page on the website (/whatsapp-subscribe) - form + QR code.
2. A WhatsApp link / QR that opens a chat with "اشتراك" already written
   (the opt-in keyword is handled in compliance.handle_keywords).
3. Consent buttons sent inside an open conversation (24-hour window):
   "Yes, I agree" / "No, thanks".
4. A personal link sent by e-mail to customers and leads.
5. A tick on the Quotation / Sales Order / Sales Invoice / Contract (signed
   clause) or on the Customer / Lead form.
6. Printable paper form for the showroom (/whatsapp-subscribe?paper=1);
   the filled forms can be recorded one by one or imported from Excel.
7. Appointment booking (appointments.py), verbal / phone (with a note).
"""

from __future__ import annotations

import io
import re
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from butler_whatsapp import compliance
from butler_whatsapp.meta import normalize_msisdn, within_service_window

CONTACT = "WhatsApp Contact"
BUSINESS = "Butler"

YES_ID = "butler_optin_yes"
NO_ID = "butler_optin_no"
YES_TITLES = ("نعم، أوافق", "Yes, I agree")
NO_TITLES = ("لا، شكراً", "No, thanks")

CLAUSE_AR = ("أوافق على أن تتواصل معي شركة باتلر للأنظمة الذكية عبر واتساب لإرسال العروض والتهاني "
	"وتحديثات الطلبات، ويمكنني إيقاف الرسائل في أي وقت بإرسال كلمة «إيقاف».")
CLAUSE_EN = ("I agree that Butler Smart Systems may contact me on WhatsApp with offers, greetings and "
	"order updates. I can stop the messages at any time by sending \"STOP\".")

QUESTION_AR = ("هل توافق على أن ترسل لك باتلر عروضها وتهانيها عبر واتساب؟ يمكنك الإيقاف في أي وقت "
	"بإرسال كلمة «إيقاف».")
QUESTION_EN = ("Do you agree that Butler may send you its offers and greetings on WhatsApp? You can stop "
	"them at any time by sending \"STOP\".")

LINK_FIELD = {"Customer": "butler_customer", "Lead": "butler_lead"}

DOC_SOURCES = {
	"Quotation": "Signed contract or quotation",
	"Sales Order": "Signed contract or quotation",
	"Sales Invoice": "Signed contract or quotation",
	"Contract": "Signed contract or quotation",
	"Customer": "Customer or lead form",
	"Lead": "Customer or lead form",
}


# ------------------------------------------------------------------- links


def business_number() -> str | None:
	"""The WhatsApp number customers write to (digits only)."""
	s = frappe.get_cached_doc("WhatsApp Inbox Settings")
	digits = re.sub(r"\D", "", s.get("display_phone") or "")
	return digits or None


def wa_link(text: str = "اشتراك") -> str | None:
	num = business_number()
	return f"https://wa.me/{num}?text={quote(text)}" if num else None


def page_link(token: str | None = None) -> str:
	return frappe.utils.get_url("/whatsapp-subscribe" + (f"?k={token}" if token else ""))


def qr_svg(text: str | None, scale: int = 5) -> str:
	"""Inline SVG QR code (empty string if the library is missing)."""
	if not text:
		return ""
	try:
		import pyqrcode
	except ImportError:
		return ""
	buf = io.BytesIO()
	pyqrcode.create(text, error="M").svg(buf, scale=scale, omithw=False, xmldecl=False, svgns=True)
	return buf.getvalue().decode()


@frappe.whitelist()
def consent_links() -> dict:
	"""Links + QR codes shown in WhatsApp Inbox Settings (to print or share)."""
	frappe.only_for(("System Manager", "WhatsApp Agent"))
	wa = wa_link()
	page = page_link()
	return {
		"page": page,
		"page_qr": qr_svg(page),
		"paper": frappe.utils.get_url("/whatsapp-subscribe?paper=1"),
		"whatsapp": wa,
		"whatsapp_qr": qr_svg(wa),
		"clause_ar": CLAUSE_AR,
		"clause_en": CLAUSE_EN,
	}


# ------------------------------------------------------- website subscription


def _token_contact(token: str | None):
	if not token or not re.fullmatch(r"[A-Za-z0-9]{10,40}", token):
		return None
	return frappe.db.get_value(CONTACT, {"butler_consent_token": token}, ["name", "contact_name", "mobile_no"],
		as_dict=True)


def page_context(token: str | None = None) -> dict:
	c = _token_contact(token)
	return {
		"token": token if c else "",
		"prefill_name": (c.contact_name if c and not re.fullmatch(r"\+?\d+", c.contact_name or "") else ""),
		"masked_number": ("+" + c.mobile_no[:-4].lstrip("+")[:5] + "•••" + c.mobile_no[-4:]) if c else "",
		"clause_ar": CLAUSE_AR,
		"clause_en": CLAUSE_EN,
		"whatsapp_link": wa_link() or "",
		"whatsapp_qr": qr_svg(wa_link()),
		"page_qr": qr_svg(page_link()),
		"page_link": page_link(),
	}


def _rate_limited(fn):
	try:
		from frappe.rate_limiter import rate_limit

		return rate_limit(limit=6, seconds=600)(fn)
	except Exception:
		return fn


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_rate_limited
def subscribe(mobile: str | None = None, full_name: str | None = None, agree: int | str = 0,
	token: str | None = None) -> dict:
	"""Subscription form of /whatsapp-subscribe."""
	if not cint(agree):
		frappe.throw(_("Please tick the consent box"))
	c = _token_contact(token)
	if c:
		num = normalize_msisdn(c.mobile_no)
		source = "Email confirmation"
	else:
		num = normalize_msisdn(mobile)
		source = "Website form"
	if not num:
		frappe.throw(_("Write a valid mobile number, e.g. 05XXXXXXXX"))
	full_name = re.sub(r"\s+", " ", (full_name or "")).strip()[:140]
	frappe.set_user("Administrator")
	try:
		from butler_whatsapp.inbox import _contact

		contact = _contact(num, create=True, display=full_name or None)
		row = frappe.db.get_value(CONTACT, contact, ["contact_name", "mobile_no"], as_dict=True)
		if full_name and (not row.contact_name or re.fullmatch(r"\+?\d+", row.contact_name)):
			frappe.db.set_value(CONTACT, contact, "contact_name", full_name)
		ip = frappe.local.request_ip if getattr(frappe.local, "request_ip", None) else "-"
		evidence = f"{_(source)} — IP {ip} — {now_datetime():%Y-%m-%d %H:%M}"
		compliance._record_consent(num, compliance.OPTED_IN, source, "Guest",
			_("The customer ticked: {0}").format(CLAUSE_AR), evidence=evidence)
		if c:
			frappe.db.set_value(CONTACT, contact, "butler_consent_token", None, update_modified=False)
		_send_welcome(num)
		frappe.db.commit()
	finally:
		frappe.set_user("Guest")
	return {"ok": True}


def _send_welcome(num: str):
	tpl = frappe.get_cached_doc("WhatsApp Inbox Settings").get("consent_welcome_template")
	if tpl:
		frappe.enqueue("butler_whatsapp.optin.send_welcome", queue="short", enqueue_after_commit=True,
			number=num, template=tpl)


def send_welcome(number: str, template: str):
	"""Optional welcome template after a new consent (the customer agreed, so it is allowed)."""
	from butler_whatsapp.inbox import send_template_core
	from butler_whatsapp.meta import count_body_variables

	frappe.set_user("Administrator")
	try:
		tpl = frappe.get_doc("WhatsApp Templates", template)
		name = frappe.db.get_value(CONTACT, {"mobile_no": ["in", [number, "+" + number]]}, "contact_name") or ""
		body = [name if not re.fullmatch(r"\+?\d*", name) else _("Customer")][: count_body_variables(tpl.template)]
		send_template_core(number, tpl, body)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="WhatsApp welcome message failed", message=frappe.get_traceback())


# ---------------------------------------------------- consent buttons in chat


@frappe.whitelist(methods=["POST"])
def ask_in_chat(number: str) -> dict:
	"""Send the consent question with two buttons (only inside the 24-hour window)."""
	from butler_whatsapp.api import _account, _client, _last_incoming
	from butler_whatsapp.inbox import _log, _require_number, get_message
	from butler_whatsapp.meta import WhatsAppError

	num = _require_number(number, "create")
	if not within_service_window(_last_incoming(num), now_datetime()):
		frappe.throw(_("The customer has not written in the last 24 hours, so the question cannot be sent now. "
			"Use another way to get the consent (link, QR code, paper form)."))
	state = compliance.get_consent(num)["state"]
	if state == compliance.OPTED_IN:
		frappe.throw(_("The customer already agreed"))
	lang = compliance.customer_language(num)
	text = QUESTION_AR if lang == "ar" else QUESTION_EN
	yes, no = (YES_TITLES[0], NO_TITLES[0]) if lang == "ar" else (YES_TITLES[1], NO_TITLES[1])
	payload = {
		"messaging_product": "whatsapp", "recipient_type": "individual", "to": num, "type": "interactive",
		"interactive": {
			"type": "button",
			"body": {"text": text},
			"action": {"buttons": [
				{"type": "reply", "reply": {"id": YES_ID, "title": yes}},
				{"type": "reply", "reply": {"id": NO_ID, "title": no}},
			]},
		},
	}
	account = _account()
	try:
		message_id = _client(account).send(payload)
	except WhatsAppError as e:
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))
	name = _log(account.name, num, message_id, "text", f"{text}\n[{yes}] [{no}]")
	return get_message(name)


def _reply_value(doc) -> str:
	"""frappe_whatsapp stores a button reply's title (or id) as the message text."""
	return re.sub(r"<[^>]+>", "", doc.get("message") or "").strip()


def handle_button_reply(doc, num: str) -> bool:
	"""The customer pressed one of the consent buttons."""
	value = _reply_value(doc)
	if not value:
		return False
	if value == YES_ID or value in YES_TITLES:
		compliance._record_consent(num, compliance.OPTED_IN, "Consent buttons in WhatsApp", "Administrator",
			_("The customer pressed: {0}").format(value),
			evidence=_("Button reply, message {0}").format(doc.get("message_id") or doc.get("name")))
		reply = compliance.customer_text(num, "opt_in_reply", value)
		if reply:
			compliance.enqueue_auto_text(num, reply)
		return True
	if value == NO_ID or value in NO_TITLES:
		from butler_whatsapp.inbox import _contact_comment

		_contact_comment(num, _("The customer declined to receive marketing messages"), by="Administrator")
		return True
	return False


# --------------------------------------------------------- consent e-mails


def _party_contact_info(doctype: str, name: str) -> tuple[str | None, str | None, str | None]:
	"""(mobile, email, display name) of a Customer or Lead."""
	if doctype == "Customer":
		row = frappe.db.get_value("Customer", name, ["customer_name", "mobile_no", "email_id"], as_dict=True)
		return (row.mobile_no, row.email_id, row.customer_name) if row else (None, None, None)
	row = frappe.db.get_value("Lead", name, ["lead_name", "company_name", "mobile_no", "email_id"], as_dict=True)
	return (row.mobile_no, row.email_id, row.lead_name or row.company_name) if row else (None, None, None)


@frappe.whitelist(methods=["POST"])
def email_consent_request(doctype: str, names: str | list) -> dict:
	"""Send each selected Customer / Lead a personal link to agree to WhatsApp messages."""
	if doctype not in ("Customer", "Lead"):
		frappe.throw(_("Only customers and leads"))
	frappe.has_permission(doctype, "read", throw=True)
	frappe.only_for(("System Manager", "WhatsApp Agent", "Sales Manager", "Sales User"))
	names = frappe.parse_json(names) if isinstance(names, str) else names
	from butler_whatsapp.inbox import _contact

	out = {"sent": 0, "no_email": 0, "no_mobile": 0, "already": 0}
	for name in names[:500]:
		mobile, email, display = _party_contact_info(doctype, name)
		num = normalize_msisdn(mobile)
		if not num:
			out["no_mobile"] += 1
			continue
		if not email:
			out["no_email"] += 1
			continue
		if compliance.get_consent(num)["state"]:
			out["already"] += 1  # agreed or asked to stop: do not ask again
			continue
		contact = _contact(num, create=True, display=display)
		if not frappe.db.get_value(CONTACT, contact, LINK_FIELD[doctype]):
			frappe.db.set_value(CONTACT, contact, LINK_FIELD[doctype], name, update_modified=False)
		token = frappe.db.get_value(CONTACT, contact, "butler_consent_token") or frappe.generate_hash(length=24)
		frappe.db.set_value(CONTACT, contact, "butler_consent_token", token, update_modified=False)
		link = page_link(token)
		frappe.sendmail(
			recipients=[email],
			subject="باتلر | تأكيد استقبال رسائل واتساب — Butler | WhatsApp messages",
			message=_email_html(display, link),
			reference_doctype=doctype, reference_name=name,
		)
		out["sent"] += 1
	return out


def _email_html(display: str | None, link: str) -> str:
	name = frappe.utils.escape_html(display or "")
	return f"""
<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;line-height:1.9">
	<p>مرحباً {name}،</p>
	<p>نود أن نرسل لك عروض باتلر للأنظمة الذكية وتهانيها وتحديثات طلباتك عبر <b>واتساب</b>.
	إن كنت توافق، اضغط الزر وأكّد موافقتك. يمكنك إيقاف الرسائل في أي وقت بإرسال كلمة «إيقاف».</p>
	<p><a href="{link}" style="background:#25d366;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none">أوافق على رسائل واتساب</a></p>
	<p style="color:#777">إن لم ترغب بذلك فتجاهل هذه الرسالة ولن نراسلك عبر واتساب.</p>
</div>
<hr>
<div dir="ltr" style="font-family:Arial,sans-serif;line-height:1.7">
	<p>Hello {name},</p>
	<p>We would like to send you Butler Smart Systems' offers, greetings and order updates on <b>WhatsApp</b>.
	If you agree, press the button and confirm. You can stop the messages at any time by sending "STOP".</p>
	<p><a href="{link}">I agree to WhatsApp messages</a></p>
	<p style="color:#777">If you do not want this, ignore this e-mail and we will not message you on WhatsApp.</p>
</div>"""


# ----------------------------------------------- consent ticked on documents


def _doc_mobile(doc) -> str | None:
	if doc.doctype == "Lead":
		for f in ("mobile_no", "whatsapp_no", "phone"):
			if doc.get(f):
				return doc.get(f)
		return None
	if doc.doctype == "Customer":
		return doc.get("mobile_no")
	if doc.doctype == "Contract":
		if doc.get("party_type") == "Customer" and doc.get("party_name"):
			return frappe.db.get_value("Customer", doc.party_name, "mobile_no")
		return None
	from butler_whatsapp.api import _default_mobile

	return _default_mobile(doc)


def on_document_consent(doc, method=None):
	"""Quotation / Sales Order / Sales Invoice (on submit), Customer / Lead / Contract (on save):
	the "customer agreed in writing" box records the consent of the document's mobile number."""
	if not cint(doc.get("butler_wa_consent")):
		return
	if method in ("on_update", "on_update_after_submit") and not doc.has_value_changed("butler_wa_consent"):
		return
	num = normalize_msisdn(_doc_mobile(doc))
	if not num:
		frappe.msgprint(_("WhatsApp consent was ticked, but there is no valid mobile number on this document."),
			indicator="orange", alert=True)
		return
	state = compliance.get_consent(num)["state"]
	if state == compliance.OPTED_IN:
		return
	if state == compliance.OPTED_OUT:
		frappe.msgprint(_("This customer had asked to stop WhatsApp messages. Ask them to send \"اشتراك\" on WhatsApp, "
			"or record the new consent from the conversation."), indicator="orange")
		return
	compliance._record_consent(num, compliance.OPTED_IN, DOC_SOURCES.get(doc.doctype, "Other"), frappe.session.user,
		_("Ticked on {0} {1}").format(_(doc.doctype), doc.name), evidence=f"{doc.doctype} {doc.name}")
	frappe.msgprint(_("WhatsApp consent recorded for +{0}").format(num), indicator="green", alert=True)
