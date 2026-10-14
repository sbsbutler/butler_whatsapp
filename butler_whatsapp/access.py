"""Who sees which WhatsApp conversation.

- Supervisors (WhatsApp Inbox Settings > Supervisors) see every conversation.
- Everybody else sees only the conversations assigned to them
  (WhatsApp Contact.butler_assigned_to). This is enforced for the inbox API
  and, through permission hooks, for the WhatsApp Message / Contact lists,
  reports and attached files.
- While no supervisor is configured nothing is restricted (fresh install).
"""

from __future__ import annotations

import frappe
from frappe import _

SETTINGS = "WhatsApp Inbox Settings"
CONTACT = "WhatsApp Contact"
MSG = "WhatsApp Message"
AGENT_ROLE = "WhatsApp Agent"


def settings():
	return frappe.get_cached_doc(SETTINGS)


def supervisors() -> set[str]:
	try:
		return {row.user for row in settings().get("supervisors") or [] if row.user}
	except Exception:
		return set()


def restricted() -> bool:
	"""Access is restricted as soon as at least one supervisor is configured."""
	return bool(supervisors())


def is_supervisor(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator" or not restricted():
		return True
	return user in supervisors()


def _clean(num: str | None) -> str:
	return (num or "").replace("+", "").strip()


def assignee(num: str) -> str | None:
	rows = frappe.db.sql(
		"""SELECT butler_assigned_to FROM `tabWhatsApp Contact`
		WHERE REPLACE(mobile_no, '+', '') = %s AND IFNULL(butler_assigned_to, '') != ''
		ORDER BY modified DESC LIMIT 1""",
		(_clean(num),),
	)
	return rows[0][0] if rows else None


def can_access(num: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if is_supervisor(user):
		return True
	return assignee(num) == user


def check_access(num: str):
	if not can_access(num):
		frappe.throw(_("This conversation is not assigned to you"), frappe.PermissionError)


def agents() -> list[dict]:
	"""Users who can work in the inbox: supervisors + WhatsApp Agent role holders."""
	users = set(supervisors())
	users |= set(
		frappe.get_all("Has Role", filters={"role": AGENT_ROLE, "parenttype": "User"}, pluck="parent")
	)
	users.discard("Administrator")
	if not users:
		return []
	rows = frappe.get_all(
		"User",
		filters={"name": ["in", list(users)], "enabled": 1, "user_type": "System User"},
		fields=["name", "full_name"],
		order_by="full_name asc",
	)
	sup = supervisors()
	return [{"user": r.name, "name": r.full_name or r.name, "supervisor": r.name in sup} for r in rows]


def notify_targets(num: str) -> list[str] | None:
	"""Users to notify about conversation `num`; None means "everyone who can read"."""
	if not restricted():
		return None
	who = assignee(num)
	sup = sorted(supervisors())
	if not who:
		return sup
	if frappe.utils.cint(settings().get("notify_supervisors")):
		return sorted(set(sup) | {who})
	return [who]


# ----------------------------------------------------------- permission hooks


def _scoped(user: str | None) -> bool:
	user = user or frappe.session.user
	return not is_supervisor(user)


def message_query(user: str | None = None) -> str:
	if not _scoped(user):
		return ""
	u = frappe.db.escape(user or frappe.session.user)
	return (
		"REPLACE(IF(`tabWhatsApp Message`.`type` = 'Incoming', `tabWhatsApp Message`.`from`, "
		"`tabWhatsApp Message`.`to`), '+', '') IN (SELECT REPLACE(mobile_no, '+', '') "
		f"FROM `tabWhatsApp Contact` WHERE butler_assigned_to = {u})"
	)


def contact_query(user: str | None = None) -> str:
	if not _scoped(user):
		return ""
	return f"`tabWhatsApp Contact`.`butler_assigned_to` = {frappe.db.escape(user or frappe.session.user)}"


def message_has_permission(doc, ptype=None, user=None, **kwargs):
	# Frappe >= 16.51 treats a None from a has_permission hook as a denial: return True to allow.
	if not _scoped(user):
		return True
	num = doc.get("from") if doc.get("type") == "Incoming" else doc.get("to")
	return True if can_access(num, user or frappe.session.user) else False


def contact_has_permission(doc, ptype=None, user=None, **kwargs):
	if not _scoped(user):
		return True
	return True if doc.get("butler_assigned_to") == (user or frappe.session.user) else False


def boot_session(bootinfo):
	if frappe.session.user == "Guest":
		return
	try:
		s = settings()
		bootinfo.butler_wa = {
			"show_launcher": frappe.utils.cint(s.get("show_launcher") if s.get("show_launcher") is not None else 1),
			"supervisor": is_supervisor(),
		}
	except Exception:
		bootinfo.butler_wa = {"show_launcher": 1, "supervisor": False}
