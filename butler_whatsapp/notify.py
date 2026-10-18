"""Alerts to staff (bell notification + phone push), each in the user's own language.

Subjects are English source strings with {0}-style placeholders; they are
translated per recipient (User.language, else the system language).
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _


def user_lang(user: str) -> str:
	try:
		from frappe.translate import get_user_lang

		return get_user_lang(user) or "en"
	except Exception:
		return "en"


def tr(msg: str, lang: str, *args) -> str:
	text = _(msg, lang=lang)
	return text.format(*args) if args else text


def by_language(users) -> dict[str, list[str]]:
	groups: dict[str, list[str]] = defaultdict(list)
	for u in sorted({u for u in users or [] if u and u not in ("Guest",)}):
		groups[user_lang(u)].append(u)
	return groups


def notify(
	users,
	subject: str,
	args: tuple | list = (),
	url: str | None = None,
	document: tuple | None = None,
	from_user: str = "Administrator",
	push: bool = True,
	bell: bool = True,
	tag: str | None = None,
):
	"""Bell notification + Web Push to `users`, translated per user."""
	for lang, group in by_language(users).items():
		text = tr(subject, lang, *args)
		if bell:
			try:
				from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

				data = {"type": "Alert", "subject": text, "from_user": from_user}
				if document:
					data.update({"document_type": document[0], "document_name": document[1]})
				enqueue_create_notification(group, data)
			except Exception:
				frappe.log_error(title="WhatsApp notification failed", message=frappe.get_traceback())
		if push:
			data = {"title": tr("WhatsApp", lang), "body": text, "url": url or "/desk/whatsapp-inbox"}
			if tag:
				data["tag"] = tag
			frappe.enqueue(
				"butler_whatsapp.push.notify_users_job",
				queue="short",
				enqueue_after_commit=True,
				users=group,
				data=data,
			)
