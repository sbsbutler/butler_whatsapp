"""Teams for the WhatsApp inbox (Sales, Maintenance, Installations, Accounts...).

- A conversation can belong to a team (WhatsApp Contact.butler_team) and/or
  be assigned to one person (butler_assigned_to). Team members see their
  team's conversations; supervisors see everything.
- New conversations are routed to a team by keywords in the customer's
  message (WhatsApp Team.keywords). Inside the team the conversation either
  stays with the whole team or goes to the next member (round robin).
- Anyone who can see a conversation can hand it over to another team or user.
  Users without the "WhatsApp Agent" role get it automatically when a
  conversation is assigned to them (or they join a team), so they can open it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from butler_whatsapp import access
from butler_whatsapp.meta import contains_keyword

TEAM = "WhatsApp Team"
MEMBER = "WhatsApp Team Member"
CACHE_KEY = "butler_wa_teams"
ROUND_ROBIN = "Round Robin"
WHOLE_TEAM = "Whole Team"

# Created on install (members are added by the admin)
DEFAULT_TEAMS = [
	{
		"team_name": "Sales",
		"priority": 10,
		"keywords": "عرض سعر\nعرض اسعار\nتسعيره\nسعر\nاسعار\nبكم\nكم السعر\nشراء\nاشتري\nابغى اركب\nquotation\nquote\nprice\nbuy",
	},
	{
		"team_name": "Maintenance",
		"priority": 20,
		"keywords": "صيانه\nعطل\nخربان\nخراب\nما يشتغل\nلا يعمل\nمشكله\nاصلاح\nتصليح\nmaintenance\nrepair\nnot working\nproblem",
	},
	{
		"team_name": "Installations",
		"priority": 30,
		"keywords": "تركيب\nموعد التركيب\nتمديد\nتوصيل\ninstallation\ninstall",
	},
	{
		"team_name": "Accounts",
		"priority": 40,
		"keywords": "فاتوره\nسداد\nدفع\nتحويل بنكي\nحوالة\nمستحقات\nاسترجاع\ninvoice\npayment\nrefund",
	},
]


# ------------------------------------------------------------------- lookup


def all_teams() -> list[dict]:
	"""Enabled teams with members, cached until a team is saved."""
	cached = frappe.cache.get_value(CACHE_KEY)
	if cached is not None:
		return cached
	if not frappe.db.table_exists(TEAM):
		return []
	teams = frappe.get_all(
		TEAM,
		filters={"enabled": 1},
		fields=["name", "team_name", "priority", "keywords", "distribution", "notify_members", "last_assigned"],
		order_by="priority asc, name asc",
	)
	members = frappe.get_all(
		MEMBER, filters={"parenttype": TEAM}, fields=["parent", "user", "is_lead", "idx"], order_by="idx asc"
	)
	enabled_users = set(frappe.get_all("User", filters={"enabled": 1}, pluck="name"))
	out = []
	for t in teams:
		out.append(
			{
				"name": t.name,
				"label": t.team_name or t.name,
				"priority": cint(t.priority),
				"keywords": t.keywords or "",
				"distribution": t.distribution or WHOLE_TEAM,
				"notify_members": cint(t.notify_members) if t.notify_members is not None else 1,
				"members": [
					{"user": m.user, "is_lead": cint(m.is_lead)}
					for m in members
					if m.parent == t.name and m.user in enabled_users
				],
			}
		)
	frappe.cache.set_value(CACHE_KEY, out, expires_in_sec=3600)
	return out


def clear_cache(*args, **kwargs):
	frappe.cache.delete_value(CACHE_KEY)


def get_team(name: str | None) -> dict | None:
	if not name:
		return None
	return next((t for t in all_teams() if t["name"] == name), None)


def members(team: str | None) -> list[str]:
	t = get_team(team)
	return [m["user"] for m in t["members"]] if t else []


def user_teams(user: str | None = None) -> list[str]:
	user = user or frappe.session.user
	return [t["name"] for t in all_teams() if any(m["user"] == user for m in t["members"])]


def is_lead(user: str, team: str | None) -> bool:
	t = get_team(team)
	return bool(t) and any(m["user"] == user and m["is_lead"] for m in t["members"])


# ------------------------------------------------------------------ routing


def route(text: str | None) -> tuple[str, str] | None:
	"""(team, matched keyword) for a customer's message, by team priority."""
	for t in all_teams():
		if not t["members"] and t["distribution"] == ROUND_ROBIN:
			continue
		kw = contains_keyword(text, t["keywords"])
		if kw:
			return t["name"], kw
	return None


def next_member(team: str) -> str | None:
	"""Round robin inside a team."""
	t = get_team(team)
	users = [m["user"] for m in t["members"]] if t else []
	if not users:
		return None
	last = frappe.db.get_value(TEAM, team, "last_assigned")
	nxt = users[(users.index(last) + 1) % len(users)] if last in users else users[0]
	frappe.db.set_value(TEAM, team, "last_assigned", nxt, update_modified=False)
	clear_cache()
	return nxt


# ------------------------------------------------------------------- roles


def ensure_agent_role(user: str | None) -> bool:
	"""Give the WhatsApp Agent role to a desk user who needs to open conversations.
	Returns True if the role was added."""
	if not user or user in ("Administrator", "Guest"):
		return False
	if access.AGENT_ROLE in frappe.get_roles(user) or user in access.supervisors():
		return False
	if frappe.db.get_value("User", user, "user_type") != "System User":
		frappe.throw(_("{0} is not a system user and cannot work in the WhatsApp inbox").format(user))
	doc = frappe.get_doc("User", user)
	doc.flags.ignore_permissions = True
	doc.add_roles(access.AGENT_ROLE)
	return True


def on_team_update(doc, method=None):
	"""WhatsApp Team on_update: members get the agent role; refresh the cache."""
	clear_cache()
	for row in doc.get("members") or []:
		if row.user:
			ensure_agent_role(row.user)


def seed_teams():
	"""Create the default teams once (the admin adds the members)."""
	if not frappe.db.table_exists(TEAM) or frappe.db.count(TEAM):
		return
	for t in DEFAULT_TEAMS:
		frappe.get_doc({"doctype": TEAM, "enabled": 1, "distribution": WHOLE_TEAM, "notify_members": 1, **t}).insert(
			ignore_permissions=True
		)
	clear_cache()


# ------------------------------------------------------------- context API


def users_for_assignment() -> list[dict]:
	"""Every enabled desk user (assignment is not limited to agents)."""
	sup = access.supervisors()
	agent = set(frappe.get_all("Has Role", filters={"role": access.AGENT_ROLE, "parenttype": "User"}, pluck="parent"))
	rows = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]},
		fields=["name", "full_name", "user_image"],
		order_by="full_name asc",
		limit=1000,
	)
	return [
		{
			"user": r.name,
			"name": r.full_name or r.name,
			"image": r.user_image,
			"supervisor": r.name in sup,
			"agent": r.name in agent or r.name in sup,
		}
		for r in rows
	]


def teams_for_context() -> list[dict]:
	return [
		{"name": t["name"], "label": _(t["label"]), "members": [m["user"] for m in t["members"]],
		 "leads": [m["user"] for m in t["members"] if m["is_lead"]]}
		for t in all_teams()
	]


# ---------------------------------------------------------- managing members


def _team_users(team: str) -> list[str]:
	return frappe.get_all(MEMBER, filters={"parent": team, "parenttype": TEAM}, pluck="user")


@frappe.whitelist(methods=["POST"])
def add_members(team: str, users: str | list) -> list[str]:
	"""Add several users to a team at once (team form / user form)."""
	frappe.has_permission(TEAM, "write", team, throw=True)
	users = frappe.parse_json(users) if isinstance(users, str) else users
	doc = frappe.get_doc(TEAM, team)
	have = {m.user for m in doc.members}
	added = []
	for user in users or []:
		if user in have or not frappe.db.exists("User", {"name": user, "enabled": 1}):
			continue
		doc.append("members", {"user": user})
		added.append(user)
	if added:
		doc.save()
	return added


@frappe.whitelist()
def get_user_teams(user: str) -> list[dict]:
	frappe.has_permission(TEAM, "read", throw=True)
	return [
		{"team": t.name, "label": _(t.name), "member": user in set(_team_users(t.name))}
		for t in frappe.get_all(TEAM, fields=["name"], order_by="priority asc")
	]


@frappe.whitelist(methods=["POST"])
def set_user_teams(user: str, teams: str | list) -> list[str]:
	"""Make the user a member of exactly these teams (user form)."""
	teams = set(frappe.parse_json(teams) if isinstance(teams, str) else (teams or []))
	for name in frappe.get_all(TEAM, pluck="name"):
		frappe.has_permission(TEAM, "write", name, throw=True)
		doc = frappe.get_doc(TEAM, name)
		rows = [m for m in doc.members if m.user == user]
		if name in teams and not rows:
			doc.append("members", {"user": user})
			doc.save()
		elif name not in teams and rows:
			doc.set("members", [m for m in doc.members if m.user != user])
			doc.save()
	return sorted(teams)
