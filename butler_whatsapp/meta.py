"""Pure helpers for the WhatsApp Cloud API (no Frappe imports, unit-tested).

Documents are uploaded to Meta's media endpoint and sent by media id, so
quotations and invoices never need a public URL on our site.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from datetime import datetime, time, timedelta

import requests

SERVICE_WINDOW = timedelta(hours=24)
_TIMEOUT = 60


class WhatsAppError(Exception):
	"""Raised when Meta returns an error for a request (`code` = Meta error code)."""

	def __init__(self, message: str, code: int | None = None):
		super().__init__(message)
		self.code = code


# Meta error codes we explain to staff (anything else shows Meta's own text).
# English source strings; callers translate them with frappe._ through `tr`.
ERROR_HINTS = {
	131047: "The 24-hour window has expired - a free-form message cannot be sent, use an approved template.",
	131026: "Not delivered: the number is not on WhatsApp, the customer's app is outdated, or they have not accepted WhatsApp's terms.",
	131049: "Not delivered: Meta is temporarily limiting marketing messages to this customer. Do not resend now.",
	131048: "Sending was limited because customers reported or blocked the number. Send less and check the number's quality.",
	131056: "Too many messages to the same number in a short time. Wait a little before retrying.",
	131051: "Unsupported message type.",
	131052: "Could not download the media sent by the customer.",
	131053: "Could not upload the file to WhatsApp (type or size not supported).",
	131031: "The account is locked by Meta - check Meta Business Manager.",
	131000: "Generic WhatsApp error, try again later.",
	130472: "The number is part of a Meta experiment and the marketing message was not delivered.",
	132000: "The number of template variables does not match the approved template.",
	132001: "The template does not exist or its language is wrong.",
	132005: "The text is too long after filling in the variables.",
	132007: "The variable content violates Meta's policy.",
	132012: "The template variable format is wrong.",
	132015: "The template is paused by Meta because of low quality.",
	132016: "The template is permanently disabled by Meta because of repeated low quality.",
	133010: "The business number is not registered with the Cloud API.",
	368: "The account is temporarily blocked for violating Meta's policies.",
	190: "The access token has expired or is invalid.",
}


def _same(text: str) -> str:
	return text


def explain_error(code, title: str | None = None, details: str | None = None, tr=_same) -> str:
	"""Explanation for a Meta error (translated with `tr`), falling back to Meta's text."""
	try:
		code = int(code) if code is not None else None
	except (TypeError, ValueError):
		code = None
	hint = ERROR_HINTS.get(code) if code is not None else None
	meta_text = " - ".join(x for x in (title, details) if x)
	if hint:
		return f"{tr(hint)} (#{code})"
	if meta_text:
		return f"{meta_text} (#{code})" if code is not None else meta_text
	return f"{tr('WhatsApp error')} (#{code})" if code is not None else tr("WhatsApp error")


def errors_for_message(payload: dict, message_id: str) -> list[dict]:
	"""Errors Meta reported for one sent message in a status webhook payload."""
	out = []
	for entry in payload.get("entry") or []:
		for change in entry.get("changes") or []:
			for st in (change.get("value") or {}).get("statuses") or []:
				if st.get("id") == message_id and st.get("errors"):
					out.extend(st["errors"])
	return out


# ---------------------------------------------------------------- webhook signature


def webhook_signature_ok(secret: str, body: bytes, header: str | None) -> bool:
	"""Meta signs the raw body with the App Secret: X-Hub-Signature-256: sha256=<hex>."""
	if not secret or not header:
		return False
	expected = "sha256=" + hmac.new(secret.encode(), body or b"", hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected, header.strip())


# ------------------------------------------------------------- opt-out keywords

_PUNCT = re.compile(r"[\s\.\!\?؟«»،؛,;:\-_ـ\u064B-\u0652]+")


def normalize_keyword(text: str | None) -> str:
	"""Lower-case, unify Arabic letter forms and drop punctuation / tashkeel,
	so 'إيقاف!' == 'ايقاف' and 'Stop' == 'STOP'."""
	t = unicodedata.normalize("NFKC", str(text or "")).strip().casefold()
	t = t.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
	return _PUNCT.sub("", t)


def keyword_list(raw: str | None) -> list[str]:
	return [k for k in (normalize_keyword(x) for x in re.split(r"[\n,،]+", raw or "")) if k]


def matches_keyword(text: str | None, keywords: list[str]) -> bool:
	"""Whole-message match only, so 'أبي إيقاف الطلب' is NOT an opt-out."""
	t = normalize_keyword(text)
	return bool(t) and t in keywords


_PREFIXES = ("وال", "بال", "لل", "ال", "و", "ب", "ل", "ف")
_WORDS = re.compile(r"[\s\.\!\?؟،؛,;:\-_ـ()\[\]\"'/«»…]+")


def _tokens(text: str | None) -> list[str]:
	t = unicodedata.normalize("NFKC", str(text or "")).casefold()
	t = re.sub(r"[\u064B-\u0652]", "", t)
	t = t.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
	return [w for w in _WORDS.split(t) if w]


def _strip_prefix(word: str) -> list[str]:
	forms = [word]
	for p in _PREFIXES:
		if word.startswith(p) and len(word) - len(p) >= 2:
			forms.append(word[len(p):])
	return forms


def contains_keyword(text: str | None, keywords_raw: str | None) -> str | None:
	"""First keyword (one per line or comma separated, may be several words)
	found as whole words anywhere in the text. Arabic prefixes like ال / و / ب
	are ignored, so 'الصيانة' matches 'صيانة'. Returns the matched keyword."""
	words = _tokens(text)
	if not words:
		return None
	variants = [set(_strip_prefix(w)) for w in words]
	for raw in re.split(r"[\n,،]+", keywords_raw or ""):
		kw = _tokens(raw)
		if not kw:
			continue
		n = len(kw)
		for i in range(len(words) - n + 1):
			if all(kw[j] in variants[i + j] for j in range(n)):
				return raw.strip()
	return None


# ---------------------------------------------------------------- business hours


def in_business_hours(now: datetime, work_days: set[int], start: time | None, end: time | None) -> bool:
	"""work_days uses Python weekday(): Monday=0 ... Sunday=6."""
	if now.weekday() not in work_days:
		return False
	if not start or not end:
		return True
	t = now.time()
	if start <= end:
		return start <= t < end
	return t >= start or t < end  # overnight shift


# --------------------------------------------------------------- template rules


def validate_template_text(body: str, samples: list[str], header: str | None = None,
	header_sample: str | None = None, footer: str | None = None, tr=_same) -> list[str]:
	"""Problems Meta would reject a new template for (empty list = OK)."""
	problems = []
	body = body or ""
	nums = [int(n) for n in re.findall(r"\{\{\s*(\d+)\s*\}\}", body)]
	distinct = sorted(set(nums))
	if not body.strip():
		problems.append(tr("The template text is empty."))
	if len(body) > 1024:
		problems.append(tr("The template text is longer than 1024 characters."))
	if distinct and distinct != list(range(1, len(distinct) + 1)):
		problems.append(tr("Variables must be consecutive, starting at {{1}}."))
	stripped = body.strip()
	if re.match(r"^\{\{\s*\d+\s*\}\}", stripped) or re.search(r"\{\{\s*\d+\s*\}\}[\s\.\!\?،]*$", stripped):
		problems.append(tr("The text may not start or end with a variable."))
	if re.search(r"\}\}\s*\{\{", body):
		problems.append(tr("Two variables may not be next to each other without text between them."))
	if len(samples) != len(distinct):
		problems.append(tr("The number of samples ({0}) does not match the number of variables ({1}).").format(len(samples), len(distinct)))
	if any("," in s for s in samples):
		problems.append(tr("Samples must not contain a comma (,)."))
	if any(not (s or "").strip() for s in samples):
		problems.append(tr("One of the samples is empty."))
	if header:
		hv = re.findall(r"\{\{\s*(\d+)\s*\}\}", header)
		if len(header) > 60:
			problems.append(tr("The header is longer than 60 characters."))
		if len(set(hv)) > 1:
			problems.append(tr("The header accepts only one variable."))
		if hv and not (header_sample or "").strip():
			problems.append(tr("The header has a variable and needs a sample."))
	if footer and len(footer) > 60:
		problems.append(tr("The footer is longer than 60 characters."))
	if footer and "{{" in footer:
		problems.append(tr("The footer does not accept variables."))
	return problems


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
	parts = ["📍 " + (loc.get("name") or "Location")]
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
			code = err.get("code")
			details = (err.get("error_data") or {}).get("details") if isinstance(err.get("error_data"), dict) else None
			msg = err.get("error_user_msg") or details or err.get("message") or f"HTTP {resp.status_code}"
			raise WhatsAppError(explain_error(code, msg) if code in ERROR_HINTS else msg, code)
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

	def mark_read(self, message_id: str) -> bool:
		"""Blue ticks on the customer's phone for this (and earlier) messages."""
		resp = self.http.post(
			f"{self.root}/messages",
			headers={**self.headers, "Content-Type": "application/json"},
			json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
			timeout=_TIMEOUT,
		)
		return bool(self._check(resp).get("success"))

	def phone_info(self) -> dict:
		"""Quality rating, messaging limit and display-name status of the number."""
		resp = self.http.get(
			self.root,
			headers=self.headers,
			params={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,name_status"},
			timeout=_TIMEOUT,
		)
		return self._check(resp)

	def list_templates(self, waba_root: str, max_pages: int = 10) -> list[dict]:
		"""All message templates of the WhatsApp Business Account (`waba_root` =
		<base>/<version>/<business_id>)."""
		url = f"{waba_root.rstrip('/')}/message_templates"
		params = {"fields": "id,name,status,category,language", "limit": 100}
		out = []
		for _ in range(max_pages):
			resp = self.http.get(url, headers=self.headers, params=params, timeout=_TIMEOUT)
			data = self._check(resp)
			out.extend(data.get("data") or [])
			nxt = (data.get("paging") or {}).get("next")
			if not nxt:
				break
			url, params = nxt, None
		return out


def business_minutes(start: datetime, end: datetime, work_days: set[int], day_start: time | None,
	day_end: time | None) -> float:
	"""Minutes between start and end that fall inside business hours
	(weekday() numbering, Monday=0). Without hours, whole work days count."""
	if end <= start:
		return 0.0
	total = 0.0
	day = start.date()
	while day <= end.date():
		if day.weekday() in work_days:
			if day_start and day_end and day_start < day_end:
				a = datetime.combine(day, day_start)
				b = datetime.combine(day, day_end)
			else:
				a = datetime.combine(day, time.min)
				b = a + timedelta(days=1)
			lo, hi = max(a, start), min(b, end)
			if hi > lo:
				total += (hi - lo).total_seconds() / 60
		day += timedelta(days=1)
	return total
