"""Live updates + notifications for incoming WhatsApp messages.

whatsapp_chat only pushes a new message to the user set in
WhatsApp Contact.email. Our contacts are shared by the whole team, so for
unassigned contacts we push the same events to every user who can read
WhatsApp Message, plus a `butler_wa_incoming` event used for desktop
notifications and the unread badge (public/js/whatsapp.bundle.js).
"""

from __future__ import annotations

import frappe
from frappe.utils import get_datetime, now, strip_html

EVENT = "butler_wa_incoming"


def on_message_insert(doc, method=None):
	"""WhatsApp Message after_insert (doc_events)."""
	if doc.type == "Outgoing" and doc.to:
		# sent from another screen (old chat, notifications...) -> refresh open inboxes
		from butler_whatsapp.inbox import publish
		from butler_whatsapp.meta import normalize_msisdn

		num = normalize_msisdn(doc.to)
		if num:
			publish(num, "outgoing", doc.name)
		return
	if doc.type != "Incoming" or not doc.get("from"):
		return
	name = doc.name
	# whatsapp_chat creates the WhatsApp Contact in its own after_insert; wait
	# for the transaction so the contact (room) exists before broadcasting.
	frappe.db.after_commit.add(lambda: _broadcast(name))
	# phone / desktop push notifications (network calls) in the background
	frappe.enqueue(
		"butler_whatsapp.push.notify_incoming",
		queue="short",
		enqueue_after_commit=True,
		message=name,
	)


def _agents() -> list[str]:
	roles = set(
		frappe.get_all("DocPerm", filters={"parent": "WhatsApp Message", "read": 1}, pluck="role")
	) | set(
		frappe.get_all("Custom DocPerm", filters={"parent": "WhatsApp Message", "read": 1}, pluck="role")
	)
	roles.discard("All")
	if not roles:
		return []
	users = frappe.get_all(
		"Has Role", filters={"role": ["in", list(roles)], "parenttype": "User"}, pluck="parent", distinct=True
	)
	return frappe.get_all(
		"User",
		filters={"name": ["in", users], "enabled": 1, "user_type": "System User"},
		pluck="name",
	) if users else []


def _preview(msg) -> str:
	if (msg.content_type or "text") == "text":
		return strip_html(msg.message or "")
	return f"[{msg.content_type}] {strip_html(msg.message or '')}".strip()


def audience(name: str) -> dict:
	"""Who should hear about incoming message `name`, and what to show them."""
	msg = frappe.get_doc("WhatsApp Message", name)
	mobile = msg.get("from")
	contact = frappe.db.get_value(
		"WhatsApp Contact", {"mobile_no": mobile}, ["name", "contact_name", "email", "creation"], as_dict=True
	)
	new_contact = bool(contact) and abs(
		(get_datetime(contact.creation) - get_datetime(msg.creation)).total_seconds()
	) < 30
	return {
		"from": mobile,
		"contact": contact,
		"new_contact": new_contact,
		"display": msg.get("profile_name") or (contact.contact_name if contact else None) or mobile,
		"content": _preview(msg),
		"recipients": [contact.email] if contact and contact.email else _agents(),
	}


def _broadcast(name: str):
	try:
		info = audience(name)
		contact, mobile = info["contact"], info["from"]
		for user in info["recipients"]:
			# Same payload whatsapp_chat sends to assigned users -> live chat update.
			if contact and not contact.email:
				payload = {
					"content": info["content"],
					"creation": now(),
					"room": contact.name,
					"contact_name": info["display"],
					"sender_user_no": mobile,
					"user": "Guest",
				}
				frappe.publish_realtime("latest_chat_updates", payload, user=user)
				frappe.publish_realtime(contact.name, payload, user=user)
			frappe.publish_realtime(
				EVENT,
				{"name": name, "room": contact.name if contact else None, "from": mobile,
				 "display": info["display"], "content": info["content"][:200],
				 "new_contact": info["new_contact"]},
				user=user,
			)
	except Exception:
		frappe.log_error(title="WhatsApp realtime broadcast failed", message=frappe.get_traceback())
