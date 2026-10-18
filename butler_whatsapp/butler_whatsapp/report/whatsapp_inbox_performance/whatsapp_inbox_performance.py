# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt
"""Per-employee WhatsApp inbox performance.

First response time = from the first customer message of a "turn" (customer
writes, nobody from staff has answered yet) to the first reply by a person.
Automatic replies (sent as Administrator) are not counted as a reply.
Times are wall-clock minutes. Non-supervisors only see their own row (team
leads see their team). Filter by team to compare the members of one team.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate, now_datetime, today

from butler_whatsapp import access, teams
from butler_whatsapp.automation import SYSTEM_USERS, waiting_conversations


def execute(filters=None):
	filters = frappe._dict(filters or {})
	start = get_datetime(getdate(filters.from_date or add_days(today(), -7)))
	end = get_datetime(add_days(getdate(filters.to_date or today()), 1))

	msgs = frappe.db.sql(
		"""
		SELECT REPLACE(IF(type = 'Incoming', `from`, `to`), '+', '') AS num, type, owner, creation
		FROM `tabWhatsApp Message`
		WHERE creation >= %s AND creation < %s AND IFNULL(content_type, '') != 'reaction'
		ORDER BY num, creation
		""",
		(start, end),
		as_dict=True,
	)

	stats = defaultdict(lambda: {"sent": 0, "replied": set(), "times": []})
	pending = {}
	for m in msgs:
		if m.type == "Incoming":
			pending.setdefault(m.num, m.creation)
			continue
		if m.owner in SYSTEM_USERS:
			continue
		s = stats[m.owner]
		s["sent"] += 1
		if m.num in pending:
			s["times"].append((get_datetime(m.creation) - get_datetime(pending.pop(m.num))).total_seconds() / 60)
			s["replied"].add(m.num)

	assigned = dict(
		frappe.db.sql(
			"""SELECT butler_assigned_to, COUNT(*) FROM `tabWhatsApp Contact`
			WHERE IFNULL(butler_assigned_to, '') != '' AND IFNULL(butler_status, 'Open') != 'Closed'
			GROUP BY butler_assigned_to"""
		)
	)
	waiting = defaultdict(int)
	longest = defaultdict(float)
	team_waiting = defaultdict(int)
	team_longest = defaultdict(float)
	now = now_datetime()
	for w in waiting_conversations(since=add_days(now, -3)):
		minutes = (now - w.waiting_since).total_seconds() / 60
		if w.assigned_to or not w.get("team"):
			key = w.assigned_to or ""
			waiting[key] += 1
			longest[key] = max(longest[key], minutes)
		else:
			team_waiting[w.team] += 1
			team_longest[w.team] = max(team_longest[w.team], minutes)

	users = set(stats) | set(assigned) | {u for u in waiting if u}
	if filters.team:
		users &= set(teams.members(filters.team))
	user_team_labels = defaultdict(list)
	for t in teams.all_teams():
		for m in t["members"]:
			user_team_labels[m["user"]].append(_(t["label"]))
	rows = []
	for user in sorted(users, key=lambda u: frappe.utils.get_fullname(u) or u):
		s = stats.get(user) or {"sent": 0, "replied": set(), "times": []}
		times = s["times"]
		rows.append(
			{
				"user": user,
				"full_name": frappe.utils.get_fullname(user) or user,
				"teams": ", ".join(user_team_labels.get(user, [])),
				"open_assigned": assigned.get(user, 0),
				"waiting": waiting.get(user, 0),
				"longest_wait": round(longest.get(user, 0)),
				"sent": s["sent"],
				"conversations": len(s["replied"]),
				"avg_first_response": round(sum(times) / len(times), 1) if times else None,
				"max_first_response": round(max(times), 1) if times else None,
			}
		)
	for t in teams.all_teams():
		if team_waiting.get(t["name"]) and (not filters.team or filters.team == t["name"]):
			rows.append({"user": "", "full_name": _("{0} (not assigned to a person)").format(_(t["label"])),
				"teams": _(t["label"]), "team": t["name"], "waiting": team_waiting[t["name"]],
				"longest_wait": round(team_longest[t["name"]])})
	if waiting.get("") and not filters.team:
		rows.append({"user": "", "full_name": _("Unassigned"), "waiting": waiting[""], "longest_wait": round(longest[""])})

	me = frappe.session.user
	if access.restricted() and not access.is_supervisor():
		lead_of = [t["name"] for t in teams.all_teams() if teams.is_lead(me, t["name"])]
		visible = {me} | {u for t in lead_of for u in teams.members(t)}
		rows = [r for r in rows if r["user"] in visible or (r.get("team") and r["team"] in teams.user_teams(me))]

	return _columns(), rows, None, _chart(rows)


def _columns():
	return [
		{"fieldname": "full_name", "label": _("Employee"), "fieldtype": "Data", "width": 180},
		{"fieldname": "teams", "label": _("Teams"), "fieldtype": "Data", "width": 150},
		{"fieldname": "open_assigned", "label": _("Open assigned conversations"), "fieldtype": "Int", "width": 150},
		{"fieldname": "waiting", "label": _("Waiting for a reply now"), "fieldtype": "Int", "width": 130},
		{"fieldname": "longest_wait", "label": _("Longest current wait (minutes)"), "fieldtype": "Int", "width": 170},
		{"fieldname": "conversations", "label": _("Conversations replied to"), "fieldtype": "Int", "width": 150},
		{"fieldname": "sent", "label": _("Messages sent"), "fieldtype": "Int", "width": 110},
		{"fieldname": "avg_first_response", "label": _("Average first reply (minutes)"), "fieldtype": "Float", "width": 150},
		{"fieldname": "max_first_response", "label": _("Slowest first reply (minutes)"), "fieldtype": "Float", "width": 150},
		{"fieldname": "user", "label": _("User"), "fieldtype": "Link", "options": "User", "width": 160},
	]


def _chart(rows):
	data = [r for r in rows if r.get("avg_first_response") is not None]
	if not data:
		return None
	return {
		"data": {
			"labels": [r["full_name"] for r in data],
			"datasets": [{"name": _("Average first reply (minutes)"), "values": [r["avg_first_response"] for r in data]}],
		},
		"type": "bar",
		"colors": ["#25d366"],
	}
