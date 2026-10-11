# Copyright (c) 2026, Butler and contributors
# For license information, please see license.txt
"""Per-employee WhatsApp inbox performance.

First response time = from the first customer message of a "turn" (customer
writes, nobody from staff has answered yet) to the first reply by a person.
Automatic replies (sent as Administrator) are not counted as a reply.
Times are wall-clock minutes. Non-supervisors only see their own row.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import add_days, get_datetime, getdate, now_datetime, today

from butler_whatsapp import access
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
	now = now_datetime()
	for w in waiting_conversations(since=add_days(now, -3)):
		key = w.assigned_to or ""
		waiting[key] += 1
		longest[key] = max(longest[key], (now - w.waiting_since).total_seconds() / 60)

	users = set(stats) | set(assigned) | {u for u in waiting if u}
	rows = []
	for user in sorted(users, key=lambda u: frappe.utils.get_fullname(u) or u):
		s = stats.get(user) or {"sent": 0, "replied": set(), "times": []}
		times = s["times"]
		rows.append(
			{
				"user": user,
				"full_name": frappe.utils.get_fullname(user) or user,
				"open_assigned": assigned.get(user, 0),
				"waiting": waiting.get(user, 0),
				"longest_wait": round(longest.get(user, 0)),
				"sent": s["sent"],
				"conversations": len(s["replied"]),
				"avg_first_response": round(sum(times) / len(times), 1) if times else None,
				"max_first_response": round(max(times), 1) if times else None,
			}
		)
	if waiting.get(""):
		rows.append({"user": "", "full_name": "غير مسندة", "waiting": waiting[""], "longest_wait": round(longest[""])})

	if access.restricted() and not access.is_supervisor():
		rows = [r for r in rows if r["user"] == frappe.session.user]

	return _columns(), rows, None, _chart(rows)


def _columns():
	return [
		{"fieldname": "full_name", "label": "الموظف", "fieldtype": "Data", "width": 180},
		{"fieldname": "open_assigned", "label": "محادثات مفتوحة مسندة", "fieldtype": "Int", "width": 150},
		{"fieldname": "waiting", "label": "بانتظار الرد الآن", "fieldtype": "Int", "width": 130},
		{"fieldname": "longest_wait", "label": "أطول انتظار حالي (دقيقة)", "fieldtype": "Int", "width": 170},
		{"fieldname": "conversations", "label": "محادثات تم الرد فيها", "fieldtype": "Int", "width": 150},
		{"fieldname": "sent", "label": "رسائل مرسلة", "fieldtype": "Int", "width": 110},
		{"fieldname": "avg_first_response", "label": "متوسط أول رد (دقيقة)", "fieldtype": "Float", "width": 150},
		{"fieldname": "max_first_response", "label": "أبطأ أول رد (دقيقة)", "fieldtype": "Float", "width": 150},
		{"fieldname": "user", "label": "المستخدم", "fieldtype": "Link", "options": "User", "width": 160},
	]


def _chart(rows):
	data = [r for r in rows if r.get("avg_first_response") is not None]
	if not data:
		return None
	return {
		"data": {
			"labels": [r["full_name"] for r in data],
			"datasets": [{"name": "متوسط أول رد (دقيقة)", "values": [r["avg_first_response"] for r in data]}],
		},
		"type": "bar",
		"colors": ["#25d366"],
	}
