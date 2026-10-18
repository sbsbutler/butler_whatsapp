"""Who sees which WhatsApp conversation.

- Supervisors (WhatsApp Inbox Settings > Supervisors) see every conversation.
- Everybody else sees the conversations assigned to them
  (WhatsApp Contact.butler_assigned_to), the conversations of their teams
  (butler_team, see teams.py) and conversations they were added to as a
  participant (WhatsApp Conversation Participant, e.g. by an @mention).
  This is enforced for the inbox API and, through permission hooks, for the
  WhatsApp Message / Contact lists, reports and attached files.
- While no supervisor is configured nothing is restricted (fresh install).
"""

from __future__ import annotations

import frappe
from frappe import _

SETTINGS = "WhatsApp Inbox Settings"
CONTACT = "WhatsApp Contact"
MSG = "WhatsApp Message"
AGENT_ROLE = "WhatsApp Agent"
PARTICIPANT = "WhatsApp Conversation Participant"
TEAM_MEMBER = "WhatsApp Team Member"


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


def _contact_row(num: str) -> dict | None:
	rows = frappe.db.sql(
		"""SELECT name, butler_assigned_to, butler_team FROM `tabWhatsApp Contact`
		WHERE REPLACE(mobile_no, '+', '') = %s
		ORDER BY IF(IFNULL(butler_assigned_to, '') != '' OR IFNULL(butler_team, '') != '', 0, 1), modified DESC
		LIMIT 1""",
		(_clean(num),),
		as_dict=True,
	) if frappe.db.has_column(CONTACT, "butler_team") else frappe.db.sql(
		"""SELECT name, butler_assigned_to, NULL AS butler_team FROM `tabWhatsApp Contact`
		WHERE REPLACE(mobile_no, '+', '') = %s ORDER BY modified DESC LIMIT 1""",
		(_clean(num),),
		as_dict=True,
	)
	return rows[0] if rows else None


def assignee(num: str) -> str | None:
	row = _contact_row(num)
	return (row or {}).get("butler_assigned_to") or None


def team_of(num: str) -> str | None:
	row = _contact_row(num)
	return (row or {}).get("butler_team") or None


def participants(num: str) -> list[str]:
	if not frappe.db.table_exists(PARTICIPANT):
		return []
	return frappe.get_all(PARTICIPANT, filters={"number": _clean(num)}, pluck="user", order_by="creation asc")


def can_access(num: str, user: str | None = None) -> bool:
	from butler_whatsapp import teams

	user = user or frappe.session.user
	if is_supervisor(user):
		return True
	row = _contact_row(num) or {}
	if row.get("butler_assigned_to") == user:
		return True
	if row.get("butler_team") and row["butler_team"] in teams.user_teams(user):
		return True
	return user in participants(num)


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
	from butler_whatsapp import teams

	if not restricted():
		return None
	who = assignee(num)
	sup = set(supervisors())
	if who:
		users = {who}
		if frappe.utils.cint(settings().get("notify_supervisors")):
			users |= sup
		return sorted(users)
	team = team_of(num)
	if team:
		users = set(teams.members(team))
		if frappe.utils.cint(settings().get("notify_supervisors")) or not users:
			users |= sup
		return sorted(users)
	return sorted(sup)


def visible_contacts_sql(user: str) -> str:
	"""SQL condition on `tabWhatsApp Contact` (alias-free) for a non-supervisor."""
	u = frappe.db.escape(user)
	parts = [f"`tabWhatsApp Contact`.butler_assigned_to = {u}"]
	if frappe.db.has_column(CONTACT, "butler_team"):
		parts.append(
			f"`tabWhatsApp Contact`.butler_team IN (SELECT parent FROM `tab{TEAM_MEMBER}` "
			f"WHERE parenttype = 'WhatsApp Team' AND user = {u})"
		)
	if frappe.db.table_exists(PARTICIPANT):
		parts.append(
			f"REPLACE(`tabWhatsApp Contact`.mobile_no, '+', '') IN (SELECT number FROM `tab{PARTICIPANT}` WHERE user = {u})"
		)
	return "(" + " OR ".join(parts) + ")"


def visible_numbers_sql(user: str) -> str:
	"""Sub-query returning the numbers (without +) a non-supervisor may see."""
	return (
		"SELECT REPLACE(`tabWhatsApp Contact`.mobile_no, '+', '') FROM `tabWhatsApp Contact` "
		f"WHERE {visible_contacts_sql(user)}"
	)


# ----------------------------------------------------------- permission hooks


def _scoped(user: str | None) -> bool:
	user = user or frappe.session.user
	return not is_supervisor(user)


def message_query(user: str | None = None) -> str:
	if not _scoped(user):
		return ""
	return (
		"REPLACE(IF(`tabWhatsApp Message`.`type` = 'Incoming', `tabWhatsApp Message`.`from`, "
		f"`tabWhatsApp Message`.`to`), '+', '') IN ({visible_numbers_sql(user or frappe.session.user)})"
	)


def contact_query(user: str | None = None) -> str:
	if not _scoped(user):
		return ""
	return visible_contacts_sql(user or frappe.session.user)


# Frappe >= 16.51 treats anything falsy (also None) returned by a has_permission
# hook as a denial, so these hooks return True to allow and False to deny.


def message_has_permission(doc, ptype=None, user=None, **kwargs):
	if not _scoped(user):
		return True
	num = doc.get("from") if doc.get("type") == "Incoming" else doc.get("to")
	return bool(can_access(num, user or frappe.session.user))


def contact_has_permission(doc, ptype=None, user=None, **kwargs):
	if not _scoped(user):
		return True
	return bool(can_access(doc.get("mobile_no") or "", user or frappe.session.user))


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
