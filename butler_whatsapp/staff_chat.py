"""Internal chat between staff inside the WhatsApp page.

Channels:
- "team:<team>"   group chat of a WhatsApp Team (members + supervisors)
- "dm:<a>|<b>"    direct messages between two users (sorted ids)

Messages ("WhatsApp Staff Message") are never sent to customers. A message
can carry a file and a link to a customer conversation ("ref_number").
Read state per user and channel is kept in "WhatsApp Staff Read".
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, get_fullname, now_datetime, strip_html

from butler_whatsapp import access, teams
from butler_whatsapp.notify import notify

STAFF_MSG = "WhatsApp Staff Message"
STAFF_READ = "WhatsApp Staff Read"
EVENT = "butler_wa_staff"


# ----------------------------------------------------------------- channels


def team_channel(team: str) -> str:
	return f"team:{team}"


def dm_channel(a: str, b: str) -> str:
	x, y = sorted([a, b])
	return f"dm:{x}|{y}"


def parse(channel: str) -> tuple[str, list[str] | str]:
	if channel.startswith("team:"):
		return "team", channel[5:]
	if channel.startswith("dm:") and "|" in channel:
		return "dm", channel[3:].split("|", 1)
	frappe.throw(_("Invalid channel"))


def channel_members(channel: str) -> set[str]:
	kind, ref = parse(channel)
	if kind == "team":
		users = set(teams.members(ref)) | set(access.supervisors())
	else:
		users = set(ref)
	users.discard("Administrator")
	return users


def can_use(channel: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	kind, ref = parse(channel)
	if kind == "team":
		if not teams.get_team(ref):
			return False
		return user in teams.members(ref) or user in access.supervisors() or user == "Administrator"
	return user in ref


def _check(channel: str):
	frappe.has_permission("WhatsApp Message", "read", throw=True)
	if not can_use(channel):
		frappe.throw(_("You are not a member of this chat"), frappe.PermissionError)


def _title(channel: str, user: str) -> str:
	kind, ref = parse(channel)
	if kind == "team":
		t = teams.get_team(ref)
		return _(t["label"]) if t else ref
	other = ref[0] if ref[1] == user else ref[1]
	return get_fullname(other) or other


def _read_row(user: str, channel: str):
	return frappe.db.get_value(STAFF_READ, {"user": user, "channel": channel}, ["name", "last_read"], as_dict=True)


def _last_read(user: str, channel: str):
	row = _read_row(user, channel)
	return row.last_read if row else None


def _mark_read(user: str, channel: str):
	row = _read_row(user, channel)
	if row:
		frappe.db.set_value(STAFF_READ, row.name, "last_read", now_datetime(), update_modified=False)
	else:
		frappe.get_doc({"doctype": STAFF_READ, "user": user, "channel": channel, "last_read": now_datetime()}).insert(
			ignore_permissions=True
		)


def _unread(user: str, channel: str) -> int:
	last = _last_read(user, channel)
	cond = {"channel": channel, "owner": ["!=", user]}
	if last:
		cond["creation"] = [">", last]
	return frappe.db.count(STAFF_MSG, cond)


# ---------------------------------------------------------------------- API


@frappe.whitelist()
def get_channels() -> list[dict]:
	"""My team chats + direct conversations, newest first, with unread counts."""
	frappe.has_permission("WhatsApp Message", "read", throw=True)
	user = frappe.session.user
	channels = []
	sup = access.is_supervisor(user)
	for t in teams.all_teams():
		if sup or any(m["user"] == user for m in t["members"]):
			channels.append(team_channel(t["name"]))
	channels += frappe.db.sql_list(
		f"SELECT DISTINCT channel FROM `tab{STAFF_MSG}` WHERE user_a = %s OR user_b = %s",
		(user, user),
	)
	out = []
	for ch in dict.fromkeys(channels):
		last = frappe.db.get_value(
			STAFF_MSG, {"channel": ch}, ["message", "owner", "creation", "attach", "ref_number"], as_dict=True,
			order_by="creation desc",
		)
		kind, ref = parse(ch)
		out.append(
			{
				"channel": ch,
				"kind": kind,
				"title": _title(ch, user),
				"other": (ref[0] if ref[1] == user else ref[1]) if kind == "dm" else None,
				"members": len(channel_members(ch)),
				"last_at": str(last.creation) if last else None,
				"last": _preview(last) if last else "",
				"last_by": get_fullname(last.owner) if last else None,
				"unread": _unread(user, ch) if last else 0,
			}
		)
	out.sort(key=lambda c: (c["last_at"] or "", c["kind"] == "team"), reverse=True)
	return out


def _preview(row) -> str:
	text = strip_html(row.get("message") or "").strip()
	if row.get("attach"):
		text = ("📎 " + text).strip()
	if row.get("ref_number"):
		text = ("💬 " + text).strip()
	return text[:120]


def _row(m) -> dict:
	return {
		"name": m.name,
		"channel": m.channel,
		"message": m.message,
		"attach": m.attach,
		"file_name": (m.attach or "").rsplit("/", 1)[-1] if m.attach else None,
		"ref_number": m.ref_number,
		"ref_title": _ref_title(m.ref_number) if m.ref_number else None,
		"owner": m.owner,
		"sender": get_fullname(m.owner) or m.owner,
		"creation": str(m.creation),
		"mine": m.owner == frappe.session.user,
	}


def _ref_title(num: str) -> str:
	from butler_whatsapp.inbox import _conversation_title

	try:
		return _conversation_title(num)
	except Exception:
		return "+" + num


@frappe.whitelist()
def get_staff_messages(channel: str, before: str | None = None, limit: int = 50) -> dict:
	_check(channel)
	limit = min(cint(limit) or 50, 200)
	filters = {"channel": channel}
	if before:
		filters["creation"] = ["<", before]
	rows = frappe.get_all(
		STAFF_MSG, filters=filters,
		fields=["name", "channel", "message", "attach", "ref_number", "owner", "creation"],
		order_by="creation desc", limit=limit + 1,
	)
	has_more = len(rows) > limit
	rows = list(reversed(rows[:limit]))
	if not before:
		_mark_read(frappe.session.user, channel)
	kind, ref = parse(channel)
	return {
		"channel": channel,
		"kind": kind,
		"title": _title(channel, frappe.session.user),
		"members": [{"user": u, "name": get_fullname(u) or u} for u in sorted(channel_members(channel))],
		"messages": [_row(r) for r in rows],
		"has_more": has_more,
	}


@frappe.whitelist(methods=["POST"])
def send_staff_message(channel: str, message: str | None = None, attach: str | None = None,
	ref_number: str | None = None) -> dict:
	_check(channel)
	text = (message or "").strip()
	if not text and not attach and not ref_number:
		frappe.throw(_("Message is empty"))
	if ref_number:
		from butler_whatsapp.meta import normalize_msisdn

		ref_number = normalize_msisdn(ref_number)
		if ref_number and not access.can_access(ref_number):
			frappe.throw(_("You cannot share a conversation you cannot open"), frappe.PermissionError)
	kind, ref = parse(channel)
	doc = frappe.get_doc(
		{
			"doctype": STAFF_MSG, "channel": channel, "message": text, "attach": attach, "ref_number": ref_number,
			"team": ref if kind == "team" else None,
			"user_a": ref[0] if kind == "dm" else None,
			"user_b": ref[1] if kind == "dm" else None,
		}
	).insert(ignore_permissions=True)
	if attach:
		file_name = frappe.db.get_value("File", {"file_url": attach, "attached_to_name": ["is", "not set"]}, "name")
		if file_name:
			frappe.db.set_value("File", file_name, {"attached_to_doctype": STAFF_MSG, "attached_to_name": doc.name})
	_mark_read(frappe.session.user, channel)
	_broadcast(doc)
	return _row(doc)


def _broadcast(doc):
	user = frappe.session.user
	members = channel_members(doc.channel)
	data = {"channel": doc.channel, "name": doc.name, "by": user}
	for u in members:
		frappe.publish_realtime(EVENT, data, user=u, after_commit=True)
	kind, _ref = parse(doc.channel)
	others = members - {user}
	if not others:
		return
	preview = _preview(doc) or _("New message")
	sender = get_fullname(user) or user
	if kind == "dm":
		notify(others, "{0}: {1}", (sender, preview), url="/desk/whatsapp-inbox?staff=" + doc.channel,
			from_user=user, bell=False, tag="butler-wa-" + doc.channel)
	else:
		notify(others, "{0} in {1}: {2}", (sender, _title(doc.channel, user), preview),
			url="/desk/whatsapp-inbox?staff=" + doc.channel, from_user=user, bell=False, tag="butler-wa-" + doc.channel)


@frappe.whitelist(methods=["POST"])
def mark_channel_read(channel: str) -> bool:
	_check(channel)
	_mark_read(frappe.session.user, channel)
	return True


@frappe.whitelist(methods=["POST"])
def open_direct(user: str) -> dict:
	frappe.has_permission("WhatsApp Message", "read", throw=True)
	if not frappe.db.exists("User", {"name": user, "enabled": 1, "user_type": "System User"}):
		frappe.throw(_("User {0} not found").format(user))
	if user == frappe.session.user:
		frappe.throw(_("Choose a colleague"))
	teams.ensure_agent_role(user)
	return {"channel": dm_channel(frappe.session.user, user)}


@frappe.whitelist()
def get_staff_unread_total() -> int:
	if not frappe.has_permission("WhatsApp Message", "read"):
		return 0
	return sum(c["unread"] for c in get_channels())


# --------------------------------------------------------- permission hooks


def staff_message_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return ""
	u = frappe.db.escape(user)
	t = f"`tab{STAFF_MSG}`"
	if access.is_supervisor(user):
		team_cond = f"IFNULL({t}.team, '') != ''"
	else:
		names = [frappe.db.escape(x) for x in teams.user_teams(user)]
		team_cond = f"{t}.team IN ({', '.join(names)})" if names else "1 = 0"
	return f"({t}.user_a = {u} OR {t}.user_b = {u} OR {team_cond} OR {t}.owner = {u})"


def staff_message_has_permission(doc, ptype=None, user=None, **kwargs):
	"""True = allowed, False = denied (Frappe >= 16.51 treats None as a denial)."""
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return True
	if ptype not in (None, "read", "print", "email", "report", "share", "export"):
		return doc.owner == user
	return bool(can_use(doc.channel, user))
