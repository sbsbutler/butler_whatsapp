"""Pure helpers for the WhatsApp Cloud API (no Frappe imports, unit-tested).

Documents are uploaded to Meta's media endpoint and sent by media id, so
quotations and invoices never need a public URL on our site.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import requests

SERVICE_WINDOW = timedelta(hours=24)
_TIMEOUT = 60


class WhatsAppError(Exception):
	"""Raised when Meta returns an error for a request."""


def normalize_msisdn(raw: str | None, default_cc: str = "966") -> str | None:
	"""Return a WhatsApp 'to' number (digits only, with country code).

	Handles the Saudi formats we see in ERPNext: '+966 50 ...', '00966...',
	'05xxxxxxxx' and bare '5xxxxxxxx'.
	"""
	if not raw:
		return None
	digits = re.sub(r"\D", "", str(raw))
	if not digits:
		return None
	if digits.startswith("00"):
		digits = digits[2:]
	elif digits.startswith("0") and len(digits) == 10:
		digits = default_cc + digits[1:]
	elif len(digits) == 9 and digits.startswith("5"):
		digits = default_cc + digits
	if len(digits) < 10 or len(digits) > 15:
		return None
	return digits


def within_service_window(last_incoming: datetime | None, now: datetime) -> bool:
	"""True if the customer wrote to us in the last 24h (free-form allowed)."""
	return bool(last_incoming) and (now - last_incoming) < SERVICE_WINDOW


def count_body_variables(template_text: str | None) -> int:
	"""Number of distinct {{n}} placeholders in a template body."""
	if not template_text:
		return 0
	return len(set(re.findall(r"\{\{\s*(\d+)\s*\}\}", template_text)))


def document_message(to: str, media_id: str, filename: str, caption: str | None = None) -> dict:
	doc = {"id": media_id, "filename": filename}
	if caption:
		doc["caption"] = caption
	return {"messaging_product": "whatsapp", "to": to, "type": "document", "document": doc}


def template_document_message(
	to: str, template: str, language: str, media_id: str, filename: str, body_params: list[str] | None = None
) -> dict:
	components = [
		{
			"type": "header",
			"parameters": [{"type": "document", "document": {"id": media_id, "filename": filename}}],
		}
	]
	if body_params:
		components.append(
			{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in body_params]}
		)
	return {
		"messaging_product": "whatsapp",
		"to": to,
		"type": "template",
		"template": {"name": template, "language": {"code": language}, "components": components},
	}


# Media types Meta accepts per message type, with their size limits (bytes).
# Anything else is sent as a document (up to 100 MB).
_MB = 1024 * 1024
MEDIA_RULES = {
	"image": ({"image/jpeg", "image/png"}, 5 * _MB),
	"video": ({"video/mp4", "video/3gpp"}, 16 * _MB),
	"audio": ({"audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"}, 16 * _MB),
}
DOCUMENT_LIMIT = 100 * _MB


def media_kind(mime: str | None, size: int) -> str:
	"""WhatsApp message type for a file: image / video / audio / document."""
	mime = (mime or "").lower().split(";")[0].strip()
	for kind, (mimes, limit) in MEDIA_RULES.items():
		if mime in mimes and size <= limit:
			return kind
	if size > DOCUMENT_LIMIT:
		raise WhatsAppError("File is larger than 100 MB (WhatsApp limit)")
	return "document"


def media_message(to: str, kind: str, media_id: str, filename: str | None = None, caption: str | None = None) -> dict:
	"""Payload for sending an uploaded media id. Audio has no caption."""
	body: dict = {"id": media_id}
	if kind == "document" and filename:
		body["filename"] = filename
	if caption and kind != "audio":
		body["caption"] = caption
	return {"messaging_product": "whatsapp", "to": to, "type": kind, kind: body}


def text_message(to: str, text: str) -> dict:
	return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text, "preview_url": True}}


def phone_tail(raw: str | None, digits: int = 9) -> str | None:
	"""Last `digits` digits of a phone number, used to match the same number
	written in different formats (+966 5x..., 05x..., 9665x...)."""
	only = re.sub(r"\D", "", str(raw or ""))
	return only[-digits:] if len(only) >= digits else None


def render_template(text: str | None, params: list | None) -> str:
	"""Fill {{1}}, {{2}}... with params (for showing what was sent)."""
	out = text or ""
	for i, value in enumerate(params or [], start=1):
		out = re.sub(r"\{\{\s*%d\s*\}\}" % i, str(value), out)
	return out


def template_message(
	to: str,
	name: str,
	language: str,
	body_params: list | None = None,
	header: dict | None = None,
) -> dict:
	"""Approved template payload.

	`header` is one Graph API header parameter, e.g. {"type": "text", "text": "..."} or
	{"type": "document", "document": {"id": ..., "filename": ...}} / {"type": "image", "image": {"id": ...}}.
	"""
	components = []
	if header:
		components.append({"type": "header", "parameters": [header]})
	if body_params:
		components.append({"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in body_params]})
	template = {"name": name, "language": {"code": language}}
	if components:
		template["components"] = components
	return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}


def with_reply(payload: dict, reply_to: str | None) -> dict:
	"""Quote an earlier message (its wamid) in the customer's WhatsApp."""
	if reply_to:
		payload = {**payload, "context": {"message_id": reply_to}}
	return payload


def describe_location(loc: dict) -> str:
	lat, lng = loc.get("latitude"), loc.get("longitude")
	parts = ["📍 " + (loc.get("name") or "موقع")]
	if loc.get("address"):
		parts.append(loc["address"])
	if lat is not None and lng is not None:
		parts.append(f"https://maps.google.com/?q={lat},{lng}")
	return "\n".join(parts)


def describe_contacts(contacts: list) -> str:
	lines = []
	for c in contacts or []:
		name = (c.get("name") or {}).get("formatted_name") or ""
		phones = ", ".join(p.get("phone") or p.get("wa_id") or "" for p in c.get("phones") or [])
		lines.append(f"👤 {name}: {phones}".strip())
	return "\n".join(lines)


class CloudAPI:
	"""Minimal Graph API client for one phone number."""

	def __init__(self, base_url: str, version: str, phone_id: str, token: str, session=None):
		self.root = f"{base_url.rstrip('/')}/{version}/{phone_id}"
		self.headers = {"Authorization": f"Bearer {token}"}
		self.http = session or requests

	def _check(self, resp) -> dict:
		try:
			data = resp.json()
		except ValueError:
			data = {}
		if resp.status_code >= 400 or "error" in data:
			err = data.get("error") or {}
			raise WhatsAppError(err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}")
		return data

	def upload_media(self, content: bytes, filename: str, mime: str = "application/pdf") -> str:
		resp = self.http.post(
			f"{self.root}/media",
			headers=self.headers,
			data={"messaging_product": "whatsapp", "type": mime},
			files={"file": (filename, content, mime)},
			timeout=_TIMEOUT,
		)
		return self._check(resp)["id"]

	def send(self, payload: dict) -> str:
		resp = self.http.post(
			f"{self.root}/messages",
			headers={**self.headers, "Content-Type": "application/json"},
			json=payload,
			timeout=_TIMEOUT,
		)
		data = self._check(resp)
		return (data.get("messages") or [{}])[0].get("id", "")
