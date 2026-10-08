"""Web Push notifications for incoming WhatsApp messages (phones + desktops).

- The service worker and the web-app manifest are served by whitelisted
  methods (Frappe does not serve .js/.json from an app's www folder). The
  `Service-Worker-Allowed: /` header lets the worker control the whole site.
- VAPID keys are generated on first use and kept encrypted in __Auth.
- Devices are stored in the "WhatsApp Push Subscription" DocType.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password, set_encrypted_password
from werkzeug.wrappers import Response

from butler_whatsapp import webpush

DOCTYPE = "WhatsApp Push Subscription"
_KEY_DOCTYPE, _KEY_NAME, _KEY_FIELD = "WhatsApp Push Subscription", "__vapid__", "vapid_private_key"
ICON_192 = "/files/butler-app-icon-192.png"
ICON_512 = "/files/butler-app-icon-512.png"
MAX_FAILURES = 5


# ----------------------------------------------------------------- keys


def _vapid_private() -> str:
	key = get_decrypted_password(_KEY_DOCTYPE, _KEY_NAME, _KEY_FIELD, raise_exception=False)
	if not key:
		key, _public = webpush.generate_vapid_keys()
		set_encrypted_password(_KEY_DOCTYPE, _KEY_NAME, key, _KEY_FIELD)
	return key


def _subject() -> str:
	return frappe.utils.get_url()


@frappe.whitelist()
def get_public_key() -> str:
	return webpush.vapid_public_key(_vapid_private())


# ------------------------------------------------- static-ish endpoints


@frappe.whitelist(allow_guest=True, methods=["GET"])
def service_worker():
	path = frappe.get_app_path("butler_whatsapp", "sw.js")
	with open(path, encoding="utf-8") as f:
		body = f.read().replace("__ICON__", ICON_192)
	return Response(
		body,
		mimetype="application/javascript",
		headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def manifest():
	name = frappe.db.get_single_value("Website Settings", "app_name") or "ERPNext"
	data = {
		"name": name,
		"short_name": name,
		"start_url": "/desk",
		"scope": "/",
		"display": "standalone",
		"background_color": "#ffffff",
		"theme_color": "#ffffff",
		"dir": "auto",
		"icons": [
			{"src": ICON_192, "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
			{"src": ICON_512, "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
		],
	}
	return Response(json.dumps(data), mimetype="application/manifest+json", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------- subscriptions


@frappe.whitelist(methods=["POST"])
def subscribe(subscription: str | dict, device: str | None = None) -> dict:
	sub = frappe.parse_json(subscription) if isinstance(subscription, str) else subscription
	endpoint = (sub or {}).get("endpoint")
	keys = (sub or {}).get("keys") or {}
	if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
		frappe.throw(_("Invalid push subscription"))
	if not webpush.is_push_endpoint(endpoint):
		frappe.throw(_("Unsupported push service"))

	existing = frappe.db.get_value(DOCTYPE, {"endpoint": endpoint}, "name")
	doc = frappe.get_doc(DOCTYPE, existing) if existing else frappe.new_doc(DOCTYPE)
	doc.update(
		{
			"user": frappe.session.user,
			"endpoint": endpoint,
			"p256dh": keys["p256dh"],
			"auth": keys["auth"],
			"device": (device or "")[:140],
			"enabled": 1,
			"failures": 0,
		}
	)
	doc.save(ignore_permissions=True)
	return {"ok": True, "name": doc.name}


@frappe.whitelist(methods=["POST"])
def unsubscribe(endpoint: str) -> dict:
	for name in frappe.get_all(DOCTYPE, filters={"endpoint": endpoint, "user": frappe.session.user}, pluck="name"):
		frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def send_test() -> dict:
	"""Push a test notification to all devices of the current user."""
	result = notify_users(
		[frappe.session.user],
		{"title": _("Butler - WhatsApp"), "body": _("Notifications are working on this device ✅"),
		 "tag": "butler-wa-test", "url": "/desk/whatsapp-inbox"},
	)
	return result


# ---------------------------------------------------------------- sending


def notify_users(users: list[str], data: dict) -> dict:
	subs = frappe.get_all(
		DOCTYPE,
		filters={"user": ["in", users], "enabled": 1},
		fields=["name", "endpoint", "p256dh", "auth", "failures"],
	) if users else []
	if not subs:
		return {"devices": 0, "sent": 0}
	key = _vapid_private()
	sent = 0
	for s in subs:
		sub = {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}}
		try:
			webpush.send(sub, data, key, _subject())
			sent += 1
			frappe.db.set_value(DOCTYPE, s.name, {"last_success": now_datetime(), "failures": 0},
				update_modified=False)
		except webpush.Gone:
			frappe.delete_doc(DOCTYPE, s.name, ignore_permissions=True, force=True)
		except Exception:
			failures = (s.failures or 0) + 1
			frappe.db.set_value(DOCTYPE, s.name, {"failures": failures, "enabled": int(failures < MAX_FAILURES)},
				update_modified=False)
			frappe.log_error(title="WhatsApp push failed", message=frappe.get_traceback())
	return {"devices": len(subs), "sent": sent}


def notify_incoming(message: str):
	"""Background job (enqueued after commit by realtime.on_message_insert)."""
	from butler_whatsapp.realtime import audience

	info = audience(message)
	notify_users(
		info["recipients"],
		{
			"title": f"WhatsApp: {info['display']}",
			"body": info["content"],
			"tag": f"butler-wa-{info['from']}",
			"url": f"/desk/whatsapp-inbox?number={(info['from'] or '').lstrip('+')}",
		},
	)
	frappe.db.commit()
