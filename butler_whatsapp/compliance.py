"""Meta policy compliance for the WhatsApp inbox.

- Consent (opt-in / opt-out) per number, stored on "WhatsApp Contact".
  Business-initiated templates need a recorded opt-in (setting), and a
  customer who opted out never gets a template again until they opt back in.
- Opt-out / opt-in keywords sent by the customer are handled automatically.
- Templates can be switched on/off locally ("butler_enabled" on WhatsApp Templates).
- Webhook requests can be verified with Meta's X-Hub-Signature-256.
- Failed deliveries get Meta's error code explained in Arabic.
- Phone quality, messaging limit and template statuses are synced hourly and
  supervisors are alerted when something degrades.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from butler_whatsapp import access
from butler_whatsapp.meta import (
	errors_for_message,
	explain_error,
	keyword_list,
	matches_keyword,
	normalize_msisdn,
	webhook_signature_ok,
	within_service_window,
)

CONTACT = "WhatsApp Contact"
MSG = "WhatsApp Message"
TEMPLATES = "WhatsApp Templates"

OPTED_IN = "موافق"
OPTED_OUT = "أوقف الرسائل"
CONSENT_SOURCES = (
	"عقد أو عرض سعر موقّع",
	"نموذج الموقع",
	"طلب العميل عبر واتساب",
	"طلب شفهي أو هاتفي",
	"أخرى",
)
SOURCE_KEYWORD = "طلب العميل عبر واتساب"

SIG_OFF = "معطّل"
SIG_LOG = "تسجيل فقط"
SIG_REJECT = "رفض غير الموقّع"
SIG_CACHE_KEY = "butler_wa_signature_last"
WEBHOOK_METHOD = "frappe_whatsapp.utils.webhook.webhook"

BAD_TEMPLATE_STATUSES = ("PAUSED", "DISABLED", "REJECTED", "FLAGGED")
BAD_QUALITY = ("YELLOW", "RED")


def _settings():
	return access.settings()


def _contact_name(num: str, create: bool = False) -> str | None:
	from butler_whatsapp.inbox import _contact

	return _contact(num, create=create)


# ------------------------------------------------------------------ consent


def get_consent(num: str) -> dict:
	contact = _contact_name(num)
	row = frappe.db.get_value(
		CONTACT, contact, ["butler_consent", "butler_consent_source", "butler_consent_at", "butler_consent_by"],
		as_dict=True,
	) if contact else None
	row = row or frappe._dict()
	return {
		"state": row.butler_consent or "",
		"source": row.butler_consent_source or "",
		"at": str(row.butler_consent_at) if row.butler_consent_at else None,
		"by": row.butler_consent_by,
	}


def _has_incoming(num: str) -> bool:
	return bool(frappe.db.exists(MSG, {"type": "Incoming", "from": ["in", [num, "+" + num]]}))


def template_is_enabled(tpl) -> bool:
	"""NULL (templates fetched before the field existed) counts as enabled."""
	value = tpl.get("butler_enabled") if hasattr(tpl, "get") else None
	return value is None or cint(value) == 1


def check_template_send(num: str, tpl) -> None:
	"""Raise if sending this template to this number would break our rules / Meta policy."""
	if not template_is_enabled(tpl):
		frappe.throw(_("Template {0} is switched off").format(tpl.get("actual_name") or tpl.name),
			title=_("Template switched off"))
	consent = get_consent(num)
	if consent["state"] == OPTED_OUT:
		frappe.throw(
			"هذا العميل طلب إيقاف رسائل باتلر، فلا يُرسل له أي قالب. "
			"يمكنك الرد عليه فقط إذا راسلكم هو خلال 24 ساعة، أو تسجيل موافقة جديدة إذا طلب العودة.",
			title="العميل أوقف الرسائل",
		)
	s = _settings()
	if not cint(s.get("require_opt_in") if s.get("require_opt_in") is not None else 1):
		return
	if consent["state"] == OPTED_IN:
		return
	from butler_whatsapp.api import _last_incoming

	if within_service_window(_last_incoming(num), now_datetime()):
		return  # customer-initiated conversation: replying is allowed
	if cint(s.get("incoming_is_opt_in")) and _has_incoming(num):
		return
	frappe.throw(
		"لا توجد موافقة مسجلة من هذا العميل على تلقي رسائل باتلر، وسياسة Meta تمنع بدء المراسلة بدونها. "
		"سجّل الموافقة (ومصدرها) من رأس المحادثة ثم أعد الإرسال.",
		title="الموافقة مطلوبة",
	)


def _record_consent(num: str, state: str, source: str | None, by: str, note: str | None = None):
	from butler_whatsapp.inbox import _contact_comment, _contact_for_link, publish

	contact = _contact_for_link(num)
	frappe.db.set_value(
		CONTACT, contact,
		{
			"butler_consent": state,
			"butler_consent_source": source or "",
			"butler_consent_at": now_datetime(),
			"butler_consent_by": by,
		},
		update_modified=True,
	)
	text = ("تسجيل موافقة العميل على تلقي الرسائل" if state == OPTED_IN else
		"العميل أوقف الرسائل" if state == OPTED_OUT else "مسح حالة الموافقة")
	if source:
		text += f" — المصدر: {source}"
	if note:
		text += f" — {frappe.utils.escape_html(note)}"
	_contact_comment(num, text, by=by)
	publish(num, "consent")


@frappe.whitelist(methods=["POST"])
def set_consent(number: str, state: str, source: str | None = None, note: str | None = None) -> dict:
	"""Record (or clear) a customer's consent from the inbox."""
	from butler_whatsapp.inbox import _require_number

	num = _require_number(number, "write")
	if state not in (OPTED_IN, OPTED_OUT, ""):
		frappe.throw(_("Invalid consent state"))
	if state == OPTED_IN and source not in CONSENT_SOURCES:
		frappe.throw("اختر مصدر الموافقة.")
	if state == "" and not access.is_supervisor():
		frappe.throw(_("Only supervisors can clear consent"), frappe.PermissionError)
	_record_consent(num, state, source if state == OPTED_IN else (source or ""), frappe.session.user, note)
	return get_consent(num)


@frappe.whitelist()
def consent_sources() -> list[str]:
	return list(CONSENT_SOURCES)


def handle_keywords(doc) -> bool:
	"""Opt-out / opt-in by the customer's own message. Returns True if handled."""
	if (doc.get("content_type") or "text") not in ("text", "button"):
		return False
	num = normalize_msisdn(doc.get("from"))
	if not num:
		return False
	s = _settings()
	text = doc.get("message")
	if matches_keyword(text, keyword_list(s.get("opt_out_keywords"))):
		_record_consent(num, OPTED_OUT, SOURCE_KEYWORD, "Administrator", f"رسالة العميل: {text}")
		reply = (s.get("opt_out_reply") or "").strip()
		if reply:
			enqueue_auto_text(num, reply)
		_alert_assignee(num, f"العميل {num} طلب إيقاف رسائل واتساب")
		return True
	if matches_keyword(text, keyword_list(s.get("opt_in_keywords"))):
		_record_consent(num, OPTED_IN, SOURCE_KEYWORD, "Administrator", f"رسالة العميل: {text}")
		reply = (s.get("opt_in_reply") or "").strip()
		if reply:
			enqueue_auto_text(num, reply)
		return True
	return False


def _alert_assignee(num: str, subject: str):
	who = access.assignee(num)
	users = {who} if who else set(access.supervisors())
	notify_users(users, subject, f"/desk/whatsapp-inbox?number={num}")


# ------------------------------------------------------- automatic messages


def enqueue_auto_text(num: str, text: str, kind: str = "auto"):
	frappe.enqueue(
		"butler_whatsapp.compliance.send_auto_text", queue="short", enqueue_after_commit=True,
		num=num, text=text, kind=kind,
	)


def send_auto_text(num: str, text: str, kind: str = "auto"):
	"""Free-form reply sent by the system (only inside the 24h window)."""
	from butler_whatsapp.api import _account, _client, _last_incoming
	from butler_whatsapp.inbox import _log
	from butler_whatsapp.meta import text_message

	frappe.set_user("Administrator")
	if not within_service_window(_last_incoming(num), now_datetime()):
		return
	try:
		account = _account()
		message_id = _client(account).send(text_message(num, text))
		_log(account.name, num, message_id, "text", text)
		if kind != "keyword":
			from butler_whatsapp.inbox import _contact

			contact = _contact(num)
			if contact:
				frappe.db.set_value(CONTACT, contact, "butler_last_auto_reply", now_datetime(), update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="WhatsApp automatic reply failed", message=frappe.get_traceback())


# --------------------------------------------------------- delivery errors


def explain_failure(doc):
	"""Called from on_message_update when an outgoing message turns "failed":
	store Meta's reason (from the raw status webhook) on the message."""
	if doc.type != "Outgoing" or (doc.get("status") or "").lower() != "failed" or not doc.get("message_id"):
		return
	if doc.get("butler_error"):
		return
	text = "فشل التسليم (لم تذكر Meta السبب)"
	try:
		rows = frappe.db.sql(
			"""SELECT meta_data FROM `tabWhatsApp Notification Log`
			WHERE template = 'Webhook' AND meta_data LIKE %s ORDER BY creation DESC LIMIT 3""",
			(f"%{doc.message_id}%",),
		)
		for (raw,) in rows:
			errors = errors_for_message(frappe.parse_json(raw) or {}, doc.message_id)
			if errors:
				e = errors[0]
				details = (e.get("error_data") or {}).get("details") if isinstance(e.get("error_data"), dict) else None
				text = explain_error(e.get("code"), e.get("title") or e.get("message"), details)
				break
	except Exception:
		frappe.log_error(title="WhatsApp: could not read failure reason", message=frappe.get_traceback())
	doc.db_set("butler_error", text, update_modified=False)


# ------------------------------------------------------- webhook signature


def verify_webhook_signature():
	"""before_request hook: check Meta's signature on webhook POSTs only."""
	req = getattr(frappe.local, "request", None)
	if not req or req.method != "POST" or WEBHOOK_METHOD not in (req.path or ""):
		return
	try:
		s = _settings()
		mode = s.get("webhook_signature_mode") or SIG_OFF
		if mode == SIG_OFF:
			return
		from frappe.utils.password import get_decrypted_password

		secret = get_decrypted_password(access.SETTINGS, access.SETTINGS, "app_secret", raise_exception=False)
		if not secret:
			_remember_signature(False, "لم يُدخل App Secret")
			return
		ok = webhook_signature_ok(secret, req.get_data(), req.headers.get("X-Hub-Signature-256"))
	except Exception:
		frappe.log_error(title="WhatsApp webhook signature check error", message=frappe.get_traceback())
		return  # never lose messages because of our own bug
	_remember_signature(ok, "صحيح" if ok else "غير صحيح")
	if not ok:
		frappe.log_error(
			title="WhatsApp webhook: invalid signature",
			message=f"mode={mode} ip={getattr(frappe.local, 'request_ip', '')}",
		)
		if mode == SIG_REJECT:
			raise frappe.PermissionError("Invalid webhook signature")


def _remember_signature(ok: bool, text: str):
	frappe.cache.set_value(SIG_CACHE_KEY, {"ok": ok, "text": text, "at": str(now_datetime())})


@frappe.whitelist()
def signature_status() -> dict | None:
	frappe.has_permission(access.SETTINGS, "read", throw=True)
	return frappe.cache.get_value(SIG_CACHE_KEY)


# ------------------------------------------------------ health / templates


def notify_users(users, subject: str, url: str | None = None, document: tuple | None = None):
	users = {u for u in users or [] if u and u not in ("Guest",)}
	if not users:
		return
	try:
		from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

		data = {"type": "Alert", "subject": subject, "from_user": "Administrator"}
		if document:
			data.update({"document_type": document[0], "document_name": document[1]})
		enqueue_create_notification(list(users), data)
	except Exception:
		frappe.log_error(title="WhatsApp notification failed", message=frappe.get_traceback())
	frappe.enqueue(
		"butler_whatsapp.push.notify_users_job", queue="short", enqueue_after_commit=True,
		users=list(users), data={"title": "واتساب", "body": subject, "url": url or "/desk/whatsapp-inbox"},
	)


def _supervisors_or_admins() -> set[str]:
	users = set(access.supervisors())
	if not users:
		users = set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"))
		users.discard("Administrator")
	return users


def sync_health():
	"""Scheduler (hourly): phone quality + template statuses from Meta."""
	from butler_whatsapp.api import _client

	name = frappe.db.get_value("WhatsApp Account", {"is_default_outgoing": 1, "status": "Active"}, "name")
	if not name:
		return
	account = frappe.get_doc("WhatsApp Account", name)
	api = _client(account)
	s = frappe.get_single(access.SETTINGS)
	alerts = []
	try:
		info = api.phone_info()
		quality = info.get("quality_rating") or ""
		tier = info.get("messaging_limit_tier") or ""
		if quality in BAD_QUALITY and quality != (s.quality_rating or ""):
			alerts.append(f"تنبيه: انخفض تقييم جودة رقم واتساب إلى {quality}. خفّفوا الرسائل غير المطلوبة وراجعوا البلاغات.")
		if s.messaging_limit_tier and tier and tier != s.messaging_limit_tier and _tier_rank(tier) < _tier_rank(s.messaging_limit_tier):
			alerts.append(f"تنبيه: انخفض حد رسائل واتساب اليومي من {s.messaging_limit_tier} إلى {tier}.")
		values = {
			"quality_rating": quality,
			"messaging_limit_tier": tier,
			"name_status": info.get("name_status") or "",
			"display_phone": info.get("display_phone_number") or "",
			"health_checked_at": now_datetime(),
			"health_error": "",
		}
	except Exception as e:
		values = {"health_checked_at": now_datetime(), "health_error": str(e)[:500]}
	for field, value in values.items():
		frappe.db.set_single_value(access.SETTINGS, field, value)

	try:
		waba_root = f"{account.url.rstrip('/')}/{account.version}/{account.business_id}"
		remote = api.list_templates(waba_root)
		alerts += _apply_template_statuses(remote, account.name)
	except Exception as e:
		frappe.db.set_single_value(access.SETTINGS, "health_error", f"القوالب: {str(e)[:400]}")

	frappe.clear_document_cache(access.SETTINGS, access.SETTINGS)
	for subject in alerts:
		notify_users(_supervisors_or_admins(), subject, "/desk/whatsapp-templates")
	frappe.db.commit()


def _tier_rank(tier: str) -> int:
	order = ["TIER_50", "TIER_250", "TIER_1K", "TIER_2K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED"]
	return order.index(tier) if tier in order else -1


def _apply_template_statuses(remote: list[dict], account: str) -> list[str]:
	alerts = []
	local = frappe.get_all(
		TEMPLATES, filters={"whatsapp_account": ["in", [account, "", None]]},
		fields=["name", "id", "actual_name", "language_code", "status", "category"],
	)
	by_id = {r.id: r for r in local if r.id}
	by_key = {(r.actual_name, (r.language_code or "").lower()): r for r in local}
	for t in remote:
		row = by_id.get(t.get("id")) or by_key.get((t.get("name"), (t.get("language") or "").lower()))
		if not row:
			continue
		changes = {}
		if t.get("status") and t["status"] != row.status:
			changes["status"] = t["status"]
			if t["status"] in BAD_TEMPLATE_STATUSES:
				alerts.append(f"قالب واتساب «{row.actual_name}» أصبح {t['status']} في Meta ولن يُرسل.")
			elif t["status"] == "APPROVED" and row.status in ("PENDING", "IN_APPEAL", "PENDING_DELETION", None, ""):
				alerts.append(f"تم اعتماد قالب واتساب «{row.actual_name}» من Meta ✅")
		if t.get("category") and t["category"] != row.category:
			changes["category"] = t["category"]
			alerts.append(f"Meta غيّرت فئة القالب «{row.actual_name}» إلى {t['category']}.")
		if changes:
			frappe.db.set_value(TEMPLATES, row.name, changes, update_modified=False)
			if "status" in changes and frappe.db.exists("DocType", "WhatsApp Template Draft"):
				for draft in frappe.get_all("WhatsApp Template Draft", filters={"whatsapp_template": row.name}, pluck="name"):
					frappe.db.set_value("WhatsApp Template Draft", draft, "meta_status", changes["status"],
						update_modified=False)
	return alerts


@frappe.whitelist(methods=["POST"])
def refresh_health() -> dict:
	frappe.only_for("System Manager")
	sync_health()
	s = frappe.get_single(access.SETTINGS)
	return {f: s.get(f) for f in ("quality_rating", "messaging_limit_tier", "name_status", "display_phone",
		"health_checked_at", "health_error")}
