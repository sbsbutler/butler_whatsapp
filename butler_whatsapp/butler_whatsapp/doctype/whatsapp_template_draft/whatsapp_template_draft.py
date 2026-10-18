# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt
"""Editable drafts of WhatsApp templates.

Staff write / edit a template here (any language, with Meta's rules checked
as they type). "Submit to Meta" creates the frappe_whatsapp "WhatsApp Templates"
record, which posts it to Meta for approval. Once approved it shows up in the
inbox automatically (status is synced hourly by compliance.sync_health).
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from butler_whatsapp.meta import validate_template_text

NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")

DRAFT = "Draft"
SUBMITTED = "Submitted"
ERROR = "Error"
LEGACY_STATUS = {"مسودة": DRAFT, "أُرسل للاعتماد": SUBMITTED, "خطأ": ERROR}
LEGACY_USAGE = {"عرض سعر": "Quotation", "أمر بيع": "Sales Order", "فاتورة": "Invoice", "موعد": "Appointment",
	"عام": "General"}


def _samples(text: str | None) -> list[str]:
	return [s.strip() for s in (text or "").splitlines() if s.strip()]


class WhatsAppTemplateDraft(Document):
	def before_insert(self):
		self.template_name = (self.template_name or "").strip().lower()

	def validate(self):
		self.template_name = (self.template_name or "").strip().lower()
		if not NAME_RE.match(self.template_name):
			frappe.throw(_("Name at Meta: lower-case English letters, digits and underscore only."))
		if self.whatsapp_template and self.has_value_changed("body"):
			frappe.throw(_("This draft was already sent to Meta. To change the approved template open it from "
				"Templates, or create a draft with a new name (e.g. _v2)."))
		self.problems = self.check()

	def check(self) -> list[str]:
		problems = validate_template_text(
			self.body or "", _samples(self.body_samples),
			header=self.header if self.header_type == "TEXT" else None,
			header_sample=self.header_sample, footer=self.footer, tr=_,
		)
		if self.header_type == "IMAGE" and not self.sample_file:
			problems.append(_("A template with an image header needs a sample image."))
		problems += button_problems(self.button_type, self.button_label, self.button_url, self.button_phone)
		return problems


def button_problems(button_type, label, url, phone) -> list[str]:
	if not button_type:
		return []
	problems = []
	if not (label or "").strip():
		problems.append(_("Enter the button text."))
	elif len(label.strip()) > 25:
		problems.append(_("The button text is longer than 25 characters."))
	if button_type == "Visit Website" and not (url or "").strip().startswith(("https://", "http://")):
		problems.append(_("Enter a website link starting with https://"))
	if button_type == "Call Phone" and not re.match(r"^\+\d{8,15}$", (phone or "").replace(" ", "")):
		problems.append(_("Enter the phone number with the country code, e.g. +966920012813"))
	return problems


@frappe.whitelist()
def check_draft(body: str, body_samples: str | None = None, header_type: str | None = None,
	header: str | None = None, header_sample: str | None = None, footer: str | None = None,
	button_type: str | None = None, button_label: str | None = None, button_url: str | None = None,
	button_phone: str | None = None) -> list[str]:
	"""Live check while editing (form script)."""
	return validate_template_text(
		body or "", _samples(body_samples), header=header if header_type == "TEXT" else None,
		header_sample=header_sample, footer=footer, tr=_,
	) + button_problems(button_type, button_label, button_url, button_phone)


def _sample_pdf(draft) -> str:
	"""A small sample PDF for DOCUMENT headers (Meta needs an example file)."""
	from frappe.utils.pdf import get_pdf

	rtl = (draft.language or "").startswith("ar")
	html = f"""<div dir="{'rtl' if rtl else 'ltr'}" style="font-family: Tahoma, Arial; padding: 40px">
		<h2>{'باتلر للأنظمة الذكية' if rtl else 'Butler Smart Systems'}</h2>
		<p>{'نموذج مستند' if rtl else 'Sample document'} — {frappe.utils.escape_html(draft.title)}</p>
		<p>{'هذا ملف مثال لاعتماد قالب واتساب.' if rtl else 'This is a sample file for WhatsApp template approval.'}</p></div>"""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{draft.template_name}_sample.pdf",
			"content": get_pdf(html),
			"is_private": 1,
			"attached_to_doctype": draft.doctype,
			"attached_to_name": draft.name,
		}
	).insert(ignore_permissions=True)
	return file_doc.file_url


@frappe.whitelist(methods=["POST"])
def submit_to_meta(name: str) -> dict:
	frappe.only_for("System Manager")
	draft = frappe.get_doc("WhatsApp Template Draft", name)
	if draft.whatsapp_template:
		frappe.throw(_("This draft was already sent."))
	problems = draft.check()
	if problems:
		frappe.throw("<br>".join(problems), title=_("The template does not follow Meta's rules"))
	if frappe.db.exists("WhatsApp Templates", {"actual_name": draft.template_name, "language_code": draft.language}):
		frappe.throw(_("A template with the same name and language exists. Change the \"Name at Meta\"."))

	sample = None
	if draft.header_type == "DOCUMENT":
		sample = draft.sample_file or _sample_pdf(draft)
	elif draft.header_type == "IMAGE":
		sample = draft.sample_file
	elif draft.header_type == "TEXT" and "{{" in (draft.header or ""):
		sample = (draft.header_sample or "").strip()  # frappe_whatsapp reads TEXT header examples from "sample"

	tpl = frappe.new_doc("WhatsApp Templates")
	tpl.update(
		{
			"template_name": draft.template_name,
			"template": draft.body,
			"language": draft.language,
			"category": draft.category,
			"header_type": draft.header_type or "",
			"header": draft.header if draft.header_type == "TEXT" else None,
			"sample": sample,
			"sample_values": ",".join(_samples(draft.body_samples)),
			"footer": draft.footer,
		}
	)
	if draft.button_type:
		tpl.append(
			"buttons",
			{
				"button_type": draft.button_type,
				"button_label": draft.button_label.strip(),
				"website_url": (draft.button_url or "").strip() if draft.button_type == "Visit Website" else None,
				"url_type": "Static" if draft.button_type == "Visit Website" else None,
				"phone_number": (draft.button_phone or "").replace(" ", "") if draft.button_type == "Call Phone" else None,
			},
		)
	frappe.db.savepoint("butler_wa_tpl")
	try:
		tpl.insert()  # after_insert posts it to Meta
	except Exception as e:
		frappe.db.rollback(save_point="butler_wa_tpl")
		message = frappe.utils.strip_html(str(e)) or _("Unknown error")
		draft.db_set({"draft_status": ERROR, "submit_error": message[:1000]})
		frappe.clear_messages()
		return {"ok": False, "error": message}
	draft.db_set({"draft_status": SUBMITTED, "whatsapp_template": tpl.name, "submit_error": "",
		"meta_status": tpl.status or "PENDING"})
	return {"ok": True, "template": tpl.name, "status": tpl.status}




# ------------------------------------------------------------- starter drafts

FOOTER = {"ar": "باتلر للأنظمة الذكية", "en": "Butler Smart Systems"}

DRAFTS = {
	"ar": [
		{
			"title": "تهنئة بمناسبة (حملة)",
			"template_name": "butler_greeting_v1",
			"category": "MARKETING",
			"usage": "General",
			"body": "مرحباً {{1}}، كل عام وأنتم بخير 🌙\nتتمنى لكم باتلر للأنظمة الذكية أياماً سعيدة وبيتاً أذكى وأكثر راحة.",
			"body_samples": "أحمد",
			"footer": "لإيقاف الرسائل أرسل: إيقاف",
			"variables_help": "1 = اسم العميل (في الحملة اكتب {name})",
		},
		{
			"title": "عرض خاص (حملة)",
			"template_name": "butler_offer_v1",
			"category": "MARKETING",
			"usage": "General",
			"body": "مرحباً {{1}}،\nلدى باتلر للأنظمة الذكية عرض خاص لك: {{2}}\nللاستفسار أو الحجز يكفي الرد على هذه الرسالة.",
			"body_samples": "أحمد\nخصم 15% على أنظمة الستائر الذكية حتى نهاية الشهر",
			"footer": "لإيقاف الرسائل أرسل: إيقاف",
			"variables_help": "1 = اسم العميل (في الحملة اكتب {name})\n2 = تفاصيل العرض",
		},
		{
			"title": "إرسال عرض سعر (PDF)",
			"template_name": "butler_quotation_v1",
			"usage": "Quotation",
			"header_type": "DOCUMENT",
			"body": "مرحباً {{1}}،\nمرفق لكم عرض السعر رقم {{2}} من باتلر بقيمة {{3}} ريال.\nللاستفسار أو تأكيد الطلب يسعدنا ردكم على هذه الرسالة.",
			"body_samples": "أحمد\nSAL-QTN-2026-00060\n15000",
			"variables_help": "1 = اسم العميل\n2 = رقم عرض السعر\n3 = المبلغ",
		},
		{
			"title": "إرسال فاتورة (PDF)",
			"template_name": "butler_invoice_v1",
			"usage": "Invoice",
			"header_type": "DOCUMENT",
			"body": "مرحباً {{1}}،\nمرفق لكم الفاتورة رقم {{2}} بمبلغ {{3}} ريال، وتاريخ استحقاقها {{4}}.\nشكراً لتعاملكم مع باتلر.",
			"body_samples": "أحمد\nACC-SINV-2026-00012\n8500\n01-11-2026",
			"variables_help": "1 = اسم العميل\n2 = رقم الفاتورة\n3 = المبلغ\n4 = تاريخ الاستحقاق",
		},
		{
			"title": "تأكيد طلب (PDF)",
			"template_name": "butler_order_confirmed_v1",
			"usage": "Sales Order",
			"header_type": "DOCUMENT",
			"body": "مرحباً {{1}}،\nتم تأكيد طلبكم رقم {{2}} بقيمة {{3}} ريال، ومرفق لكم نسخة منه.\nسيتواصل معكم فريق باتلر لتحديد موعد التركيب.",
			"body_samples": "أحمد\nSAL-ORD-2026-00031\n15000",
			"variables_help": "1 = اسم العميل\n2 = رقم أمر البيع\n3 = المبلغ",
		},
		{
			"title": "تأكيد موعد زيارة فريق باتلر",
			"template_name": "butler_visit_v1",
			"usage": "Appointment",
			"body": "مرحباً {{1}}،\nنؤكد موعد زيارة فريق باتلر يوم {{2}} الساعة {{3}} في {{4}}.\nلتعديل الموعد يرجى الرد على هذه الرسالة.",
			"body_samples": "أحمد\nالأحد 12 أكتوبر\n10:00 صباحاً\nحي النسيم",
			"variables_help": "1 = اسم العميل\n2 = اليوم والتاريخ\n3 = الوقت\n4 = الموقع",
		},
		{
			"title": "تأكيد موعد زيارة المعرض",
			"template_name": "butler_showroom_visit_v1",
			"usage": "Appointment",
			"body": "مرحباً {{1}}،\nتم تأكيد موعد زيارتك لمعرض باتلر للأنظمة الذكية يوم {{2}} الساعة {{3}}.\nموقع المعرض: {{4}}\nنسعد باستقبالك، ولتعديل الموعد أو إلغائه يكفي الرد على هذه الرسالة.",
			"body_samples": "أحمد\nالأحد 18-10-2026\n4:30 مساءً\nhttps://maps.app.goo.gl/ButlerShowroom",
			"variables_help": "1 = اسم العميل\n2 = اليوم والتاريخ\n3 = الوقت\n4 = رابط موقع المعرض (من الإعدادات)",
		},
		{
			"title": "تذكير بموعد زيارة المعرض",
			"template_name": "butler_showroom_reminder_v1",
			"usage": "Appointment",
			"body": "مرحباً {{1}}،\nنذكّرك بموعد زيارتك لمعرض باتلر للأنظمة الذكية يوم {{2}} الساعة {{3}}.\nموقع المعرض: {{4}}\nإذا رغبت بتغيير الموعد يرجى الرد على هذه الرسالة.",
			"body_samples": "أحمد\nالأحد 18-10-2026\n4:30 مساءً\nhttps://maps.app.goo.gl/ButlerShowroom",
			"variables_help": "1 = اسم العميل\n2 = اليوم والتاريخ\n3 = الوقت\n4 = رابط موقع المعرض (من الإعدادات)",
		},
		{
			"title": "تحديث حالة طلب",
			"template_name": "butler_order_update_v1",
			"usage": "General",
			"body": "مرحباً {{1}}،\nتحديث بخصوص طلبكم رقم {{2}}: {{3}}.\nللاستفسار يرجى الرد على هذه الرسالة.",
			"body_samples": "أحمد\nSAL-ORD-2026-00031\nتم توريد الأجهزة وسيبدأ التركيب خلال يومين",
			"variables_help": "1 = اسم العميل\n2 = رقم الطلب\n3 = التحديث",
		},
		{
			"title": "اكتمال خدمة",
			"template_name": "butler_service_done_v1",
			"usage": "General",
			"body": "مرحباً {{1}}،\nتم الانتهاء من {{2}} بنجاح.\nإذا احتجتم أي مساعدة يسعدنا تواصلكم بالرد على هذه الرسالة.",
			"body_samples": "أحمد\nصيانة نظام الستائر الذكية",
			"variables_help": "1 = اسم العميل\n2 = الخدمة",
		},
		{
			"title": "متابعة طلب عميل (لفتح المحادثة بعد 24 ساعة)",
			"template_name": "butler_follow_up_v1",
			"usage": "General",
			"body": "مرحباً {{1}}،\nنتواصل معكم من باتلر بخصوص {{2}}.\nيرجى الرد على هذه الرسالة لنكمل خدمتكم.",
			"body_samples": "أحمد\nطلب صيانة نظام الإضاءة",
			"variables_help": "1 = اسم العميل\n2 = الموضوع",
		},
	],
	"en": [
		{
			"title": "Season's greetings (campaign)",
			"template_name": "butler_greeting_v1",
			"category": "MARKETING",
			"usage": "General",
			"body": "Hello {{1}}, warm wishes for the season 🌙\nButler Smart Systems wishes you happy days and a smarter, more comfortable home.",
			"body_samples": "Ahmed",
			"footer": "To stop these messages send: STOP",
			"variables_help": "1 = customer name (in a campaign write {name})",
		},
		{
			"title": "Special offer (campaign)",
			"template_name": "butler_offer_v1",
			"category": "MARKETING",
			"usage": "General",
			"body": "Hello {{1}},\nButler Smart Systems has a special offer for you: {{2}}\nTo ask or book, simply reply to this message.",
			"body_samples": "Ahmed\n15% off smart curtain systems until the end of the month",
			"footer": "To stop these messages send: STOP",
			"variables_help": "1 = customer name (in a campaign write {name})\n2 = offer details",
		},
		{
			"title": "Send quotation (PDF)",
			"template_name": "butler_quotation_v1",
			"usage": "Quotation",
			"header_type": "DOCUMENT",
			"body": "Hello {{1}},\nPlease find attached quotation {{2}} from Butler for SAR {{3}}.\nFor any questions or to confirm the order, simply reply to this message.",
			"body_samples": "Ahmed\nSAL-QTN-2026-00060\n15000",
			"variables_help": "1 = customer name\n2 = quotation number\n3 = amount",
		},
		{
			"title": "Send invoice (PDF)",
			"template_name": "butler_invoice_v1",
			"usage": "Invoice",
			"header_type": "DOCUMENT",
			"body": "Hello {{1}},\nPlease find attached invoice {{2}} for SAR {{3}}, due on {{4}}.\nThank you for choosing Butler.",
			"body_samples": "Ahmed\nACC-SINV-2026-00012\n8500\n01-11-2026",
			"variables_help": "1 = customer name\n2 = invoice number\n3 = amount\n4 = due date",
		},
		{
			"title": "Order confirmation (PDF)",
			"template_name": "butler_order_confirmed_v1",
			"usage": "Sales Order",
			"header_type": "DOCUMENT",
			"body": "Hello {{1}},\nYour order {{2}} for SAR {{3}} is confirmed and a copy is attached.\nThe Butler team will contact you to schedule the installation.",
			"body_samples": "Ahmed\nSAL-ORD-2026-00031\n15000",
			"variables_help": "1 = customer name\n2 = sales order number\n3 = amount",
		},
		{
			"title": "Butler team visit confirmation",
			"template_name": "butler_visit_v1",
			"usage": "Appointment",
			"body": "Hello {{1}},\nWe confirm the Butler team's visit on {{2}} at {{3}} in {{4}}.\nTo change the appointment, please reply to this message.",
			"body_samples": "Ahmed\nSunday 12 October\n10:00 AM\nAl Naseem district",
			"variables_help": "1 = customer name\n2 = day and date\n3 = time\n4 = location",
		},
		{
			"title": "Showroom visit confirmation",
			"template_name": "butler_showroom_visit_v1",
			"usage": "Appointment",
			"body": "Hello {{1}},\nYour visit to the Butler Smart Systems showroom is confirmed for {{2}} at {{3}}.\nShowroom location: {{4}}\nWe look forward to welcoming you. To change or cancel, simply reply to this message.",
			"body_samples": "Ahmed\nSunday 18-10-2026\n4:30 PM\nhttps://maps.app.goo.gl/ButlerShowroom",
			"variables_help": "1 = customer name\n2 = day and date\n3 = time\n4 = showroom location link (from the settings)",
		},
		{
			"title": "Showroom visit reminder",
			"template_name": "butler_showroom_reminder_v1",
			"usage": "Appointment",
			"body": "Hello {{1}},\nThis is a reminder of your visit to the Butler Smart Systems showroom on {{2}} at {{3}}.\nShowroom location: {{4}}\nTo change the time, please reply to this message.",
			"body_samples": "Ahmed\nSunday 18-10-2026\n4:30 PM\nhttps://maps.app.goo.gl/ButlerShowroom",
			"variables_help": "1 = customer name\n2 = day and date\n3 = time\n4 = showroom location link (from the settings)",
		},
		{
			"title": "Order status update",
			"template_name": "butler_order_update_v1",
			"usage": "General",
			"body": "Hello {{1}},\nAn update on your order {{2}}: {{3}}.\nFor any questions, please reply to this message.",
			"body_samples": "Ahmed\nSAL-ORD-2026-00031\nThe devices were delivered and installation starts within two days",
			"variables_help": "1 = customer name\n2 = order number\n3 = update",
		},
		{
			"title": "Service completed",
			"template_name": "butler_service_done_v1",
			"usage": "General",
			"body": "Hello {{1}},\nWe have completed {{2}} successfully.\nIf you need any help, simply reply to this message.",
			"body_samples": "Ahmed\nSmart curtain maintenance",
			"variables_help": "1 = customer name\n2 = service",
		},
		{
			"title": "Customer follow-up (reopen the conversation after 24 hours)",
			"template_name": "butler_follow_up_v1",
			"usage": "General",
			"body": "Hello {{1}},\nThis is Butler following up on {{2}}.\nPlease reply to this message so we can continue serving you.",
			"body_samples": "Ahmed\nyour lighting maintenance request",
			"variables_help": "1 = customer name\n2 = subject",
		},
	],
}


def seed_drafts():
	"""Create the starter drafts once per language (never overwrites staff edits)."""
	if not frappe.db.table_exists("WhatsApp Template Draft"):
		return
	for language, drafts in DRAFTS.items():
		if not frappe.db.exists("Language", language):
			continue
		for d in drafts:
			if frappe.db.exists("WhatsApp Template Draft", {"template_name": d["template_name"], "language": language}):
				continue
			doc = frappe.get_doc({"doctype": "WhatsApp Template Draft", "category": "UTILITY", "language": language,
				"footer": FOOTER[language], "draft_status": DRAFT, **d})  # a draft may set its own category / footer
			doc.insert(ignore_permissions=True)
