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
	if is_duplicate(doc):
		# Meta re-sent a webhook we already stored (it retries when our answer is slow,
		# e.g. while a video downloads): no second notification, and the copy is removed
		# once frappe_whatsapp has finished with it.
		frappe.enqueue(
			"butler_whatsapp.realtime.remove_duplicate", queue="short", enqueue_after_commit=True, name=doc.name
		)
		return
	try:
		# reopen / auto-assign / location text, in this transaction
		from butler_whatsapp.inbox import on_incoming

		on_incoming(doc)
	except Exception:
		frappe.log_error(title="WhatsApp incoming hook failed", message=frappe.get_traceback())
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


def is_duplicate(doc) -> bool:
	return bool(doc.get("message_id")) and bool(
		frappe.db.exists(
			"WhatsApp Message",
			{"message_id": doc.message_id, "type": "Incoming", "name": ["!=", doc.name], "creation": ["<=", doc.creation]},
		)
	)


def remove_duplicate(name: str):
	"""Delete a duplicate incoming message and its file record (the file on disk is
	kept when the original message still uses it)."""
	if not frappe.db.exists("WhatsApp Message", name):
		return
	for f in frappe.get_all("File", filters={"attached_to_doctype": "WhatsApp Message", "attached_to_name": name},
		pluck="name"):
		frappe.delete_doc("File", f, ignore_permissions=True, force=True)
	frappe.delete_doc("WhatsApp Message", name, ignore_permissions=True, force=True)
	frappe.db.commit()


def remove_existing_duplicates() -> int:
	"""Clean up duplicates stored before this check existed (after_migrate)."""
	rows = frappe.db.sql(
		"""SELECT name FROM `tabWhatsApp Message` m
		WHERE m.type = 'Incoming' AND IFNULL(m.message_id, '') != '' AND EXISTS (
			SELECT 1 FROM `tabWhatsApp Message` o WHERE o.message_id = m.message_id AND o.type = 'Incoming'
			AND (o.creation < m.creation OR (o.creation = m.creation AND o.name < m.name)))""",
		pluck=True,
	)
	for name in rows:
		remove_duplicate(name)
	return len(rows)


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
		"recipients": _recipients(mobile, contact),
	}


def _recipients(mobile, contact) -> list[str]:
	from butler_whatsapp.access import notify_targets

	targets = notify_targets(mobile)
	if targets is not None:
		return targets
	return [contact.email] if contact and contact.email else _agents()


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
