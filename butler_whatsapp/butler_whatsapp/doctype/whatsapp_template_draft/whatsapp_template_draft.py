# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt
"""Editable drafts of WhatsApp templates.

Staff write / edit a template here (Arabic, with Meta's rules checked as they
type). "Submit to Meta" creates the frappe_whatsapp "WhatsApp Templates"
record, which posts it to Meta for approval. Once approved it shows up in the
inbox automatically (status is synced hourly by compliance.sync_health).
"""

from __future__ import annotations

import re

import frappe
from frappe.model.document import Document

from butler_whatsapp.meta import validate_template_text

NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")


def _samples(text: str | None) -> list[str]:
	return [s.strip() for s in (text or "").splitlines() if s.strip()]


class WhatsAppTemplateDraft(Document):
	def before_insert(self):
		self.template_name = (self.template_name or "").strip().lower()

	def validate(self):
		self.template_name = (self.template_name or "").strip().lower()
		if not NAME_RE.match(self.template_name):
			frappe.throw("الاسم في Meta: أحرف إنجليزية صغيرة وأرقام وشرطة سفلية فقط.")
		if self.whatsapp_template and self.has_value_changed("body"):
			frappe.throw("أُرسلت هذه المسودة إلى Meta. لتعديل القالب المعتمد افتحه من «القوالب»، أو أنشئ مسودة باسم جديد (مثل _v2).")
		self.problems = self.check()

	def check(self) -> list[str]:
		problems = validate_template_text(
			self.body or "", _samples(self.body_samples),
			header=self.header if self.header_type == "TEXT" else None,
			header_sample=self.header_sample, footer=self.footer,
		)
		if self.header_type == "IMAGE" and not self.sample_file:
			problems.append("القالب بعنوان صورة يحتاج صورة مثال.")
		return problems


@frappe.whitelist()
def check_draft(body: str, body_samples: str | None = None, header_type: str | None = None,
	header: str | None = None, header_sample: str | None = None, footer: str | None = None) -> list[str]:
	"""Live check while editing (form script)."""
	return validate_template_text(
		body or "", _samples(body_samples), header=header if header_type == "TEXT" else None,
		header_sample=header_sample, footer=footer,
	)


def _sample_pdf(draft) -> str:
	"""A small sample PDF for DOCUMENT headers (Meta needs an example file)."""
	from frappe.utils.pdf import get_pdf

	html = f"""<div dir="rtl" style="font-family: Tahoma, Arial; padding: 40px">
		<h2>باتلر للأنظمة الذكية</h2>
		<p>نموذج مستند — {frappe.utils.escape_html(draft.title)}</p>
		<p>هذا ملف مثال لاعتماد قالب واتساب.</p></div>"""
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
		frappe.throw("أُرسلت هذه المسودة من قبل.")
	problems = draft.check()
	if problems:
		frappe.throw("<br>".join(problems), title="القالب لا يطابق قواعد Meta")
	if frappe.db.exists("WhatsApp Templates", {"actual_name": draft.template_name, "language_code": draft.language}):
		frappe.throw("يوجد قالب بنفس الاسم واللغة. غيّر «الاسم في Meta».")

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
	frappe.db.savepoint("butler_wa_tpl")
	try:
		tpl.insert()  # after_insert posts it to Meta
	except Exception as e:
		frappe.db.rollback(save_point="butler_wa_tpl")
		message = frappe.utils.strip_html(str(e)) or "خطأ غير معروف"
		draft.db_set({"draft_status": "خطأ", "submit_error": message[:1000]})
		frappe.clear_messages()
		return {"ok": False, "error": message}
	draft.db_set({"draft_status": "أُرسل للاعتماد", "whatsapp_template": tpl.name, "submit_error": "",
		"meta_status": tpl.status or "PENDING"})
	return {"ok": True, "template": tpl.name, "status": tpl.status}


# ------------------------------------------------------------- starter drafts

FOOTER = "باتلر للأنظمة الذكية"

DRAFTS = [
	{
		"title": "إرسال عرض سعر (PDF)",
		"template_name": "butler_quotation_v1",
		"usage": "عرض سعر",
		"header_type": "DOCUMENT",
		"body": "مرحباً {{1}}،\nمرفق لكم عرض السعر رقم {{2}} من باتلر بقيمة {{3}} ريال.\nللاستفسار أو تأكيد الطلب يسعدنا ردكم على هذه الرسالة.",
		"body_samples": "أحمد\nSAL-QTN-2026-00060\n15000",
		"variables_help": "1 = اسم العميل\n2 = رقم عرض السعر\n3 = المبلغ",
	},
	{
		"title": "إرسال فاتورة (PDF)",
		"template_name": "butler_invoice_v1",
		"usage": "فاتورة",
		"header_type": "DOCUMENT",
		"body": "مرحباً {{1}}،\nمرفق لكم الفاتورة رقم {{2}} بمبلغ {{3}} ريال، وتاريخ استحقاقها {{4}}.\nشكراً لتعاملكم مع باتلر.",
		"body_samples": "أحمد\nACC-SINV-2026-00012\n8500\n01-11-2026",
		"variables_help": "1 = اسم العميل\n2 = رقم الفاتورة\n3 = المبلغ\n4 = تاريخ الاستحقاق",
	},
	{
		"title": "تأكيد طلب (PDF)",
		"template_name": "butler_order_confirmed_v1",
		"usage": "أمر بيع",
		"header_type": "DOCUMENT",
		"body": "مرحباً {{1}}،\nتم تأكيد طلبكم رقم {{2}} بقيمة {{3}} ريال، ومرفق لكم نسخة منه.\nسيتواصل معكم فريق باتلر لتحديد موعد التركيب.",
		"body_samples": "أحمد\nSAL-ORD-2026-00031\n15000",
		"variables_help": "1 = اسم العميل\n2 = رقم أمر البيع\n3 = المبلغ",
	},
	{
		"title": "تأكيد موعد زيارة",
		"template_name": "butler_visit_v1",
		"usage": "موعد",
		"body": "مرحباً {{1}}،\nنؤكد موعد زيارة فريق باتلر يوم {{2}} الساعة {{3}} في {{4}}.\nلتعديل الموعد يرجى الرد على هذه الرسالة.",
		"body_samples": "أحمد\nالأحد 12 أكتوبر\n10:00 صباحاً\nحي النسيم",
		"variables_help": "1 = اسم العميل\n2 = اليوم والتاريخ\n3 = الوقت\n4 = الموقع",
	},
	{
		"title": "تحديث حالة طلب",
		"template_name": "butler_order_update_v1",
		"usage": "عام",
		"body": "مرحباً {{1}}،\nتحديث بخصوص طلبكم رقم {{2}}: {{3}}.\nللاستفسار يرجى الرد على هذه الرسالة.",
		"body_samples": "أحمد\nSAL-ORD-2026-00031\nتم توريد الأجهزة وسيبدأ التركيب خلال يومين",
		"variables_help": "1 = اسم العميل\n2 = رقم الطلب\n3 = التحديث",
	},
	{
		"title": "اكتمال خدمة",
		"template_name": "butler_service_done_v1",
		"usage": "عام",
		"body": "مرحباً {{1}}،\nتم الانتهاء من {{2}} بنجاح.\nإذا احتجتم أي مساعدة يسعدنا تواصلكم بالرد على هذه الرسالة.",
		"body_samples": "أحمد\nصيانة نظام الستائر الذكية",
		"variables_help": "1 = اسم العميل\n2 = الخدمة",
	},
	{
		"title": "متابعة طلب عميل (لفتح المحادثة بعد 24 ساعة)",
		"template_name": "butler_follow_up_v1",
		"usage": "عام",
		"body": "مرحباً {{1}}،\nنتواصل معكم من باتلر بخصوص {{2}}.\nيرجى الرد على هذه الرسالة لنكمل خدمتكم.",
		"body_samples": "أحمد\nطلب صيانة نظام الإضاءة",
		"variables_help": "1 = اسم العميل\n2 = الموضوع",
	},
]


def seed_drafts():
	"""Create the starter drafts once (never overwrites staff edits)."""
	if not frappe.db.table_exists("WhatsApp Template Draft"):
		return
	language = "ar" if frappe.db.exists("Language", "ar") else None
	if not language:
		return
	for d in DRAFTS:
		if frappe.db.exists("WhatsApp Template Draft", d["template_name"]):
			continue
		doc = frappe.get_doc({"doctype": "WhatsApp Template Draft", "category": "UTILITY", "language": language,
			"footer": FOOTER, **d})
		doc.insert(ignore_permissions=True)
