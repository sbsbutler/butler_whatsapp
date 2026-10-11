"""Automatic replies and response-time (SLA) alerts for the WhatsApp inbox.

Everything here replies only inside the 24h window the customer opened by
writing to us, so it never needs a template and stays within Meta policy.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, get_time, now_datetime

from butler_whatsapp import access
from butler_whatsapp.meta import business_minutes, in_business_hours, normalize_msisdn

MSG = "WhatsApp Message"
CONTACT = "WhatsApp Contact"
SYSTEM_USERS = ("Administrator", "Guest")

# Settings check fields -> Python weekday() (Monday = 0)
DAY_FIELDS = {"day_mon": 0, "day_tue": 1, "day_wed": 2, "day_thu": 3, "day_fri": 4, "day_sat": 5, "day_sun": 6}
DEFAULT_DAYS = {6, 0, 1, 2, 3}  # Sunday - Thursday


def work_schedule(s=None):
	s = s or access.settings()
	days = {wd for field, wd in DAY_FIELDS.items() if cint(s.get(field))}
	if not any(s.get(f) is not None for f in DAY_FIELDS):
		days = set(DEFAULT_DAYS)
	start = get_time(s.get("work_start")) if s.get("work_start") else None
	end = get_time(s.get("work_end")) if s.get("work_end") else None
	return days, start, end


def is_open_now(s=None) -> bool:
	days, start, end = work_schedule(s)
	return in_business_hours(now_datetime(), days, start, end)


# ------------------------------------------------------------ auto replies


def on_incoming(doc):
	"""Welcome message for a brand-new number, "we're closed" outside business hours."""
	from butler_whatsapp.compliance import enqueue_auto_text

	s = access.settings()
	if not (cint(s.get("welcome_enabled")) or cint(s.get("away_enabled"))):
		return
	if (doc.get("content_type") or "text") in ("reaction",):
		return
	num = normalize_msisdn(doc.get("from"))
	if not num:
		return
	parts = []
	if cint(s.get("welcome_enabled")) and (s.get("welcome_message") or "").strip():
		count = frappe.db.count(MSG, {"type": "Incoming", "from": ["in", [num, "+" + num]]})
		if count <= 1:
			parts.append(s.welcome_message.strip())
	if cint(s.get("away_enabled")) and (s.get("away_message") or "").strip() and not is_open_now(s):
		if _cooldown_passed(num, cint(s.get("away_cooldown_hours")) or 12):
			parts.append(s.away_message.strip())
	if parts:
		enqueue_auto_text(num, "\n\n".join(parts))


def _cooldown_passed(num: str, hours: int) -> bool:
	from butler_whatsapp.inbox import _contact

	contact = _contact(num)
	last = frappe.db.get_value(CONTACT, contact, "butler_last_auto_reply") if contact else None
	return not last or now_datetime() - get_datetime(last) >= timedelta(hours=hours)


# --------------------------------------------------------------------- SLA


def check_sla():
	"""Scheduler (every 10 min): alert when a customer waits too long for a human reply."""
	s = access.settings()
	limit = cint(s.get("sla_minutes"))
	if limit <= 0:
		return
	business_only = cint(s.get("sla_business_hours_only")) if s.get("sla_business_hours_only") is not None else 1
	days, start, end = work_schedule(s)
	now = now_datetime()
	if business_only and not in_business_hours(now, days, start, end):
		return
	for row in waiting_conversations(since=now - timedelta(days=3)):
		waited = business_minutes(row.waiting_since, now, days, start, end) if business_only else \
			(now - row.waiting_since).total_seconds() / 60
		if waited < limit:
			continue
		if row.contact and frappe.db.get_value(CONTACT, row.contact, "butler_sla_alerted") == row.first_message:
			continue
		_alert(row, int(waited), cint(s.get("sla_notify_supervisors")) if s.get("sla_notify_supervisors") is not None else 1)
		if row.contact:
			frappe.db.set_value(CONTACT, row.contact, "butler_sla_alerted", row.first_message, update_modified=False)
	frappe.db.commit()


def waiting_conversations(since):
	"""Open conversations whose newest customer message has no human reply after it."""
	rows = frappe.db.sql(
		"""
		SELECT REPLACE(m.`from`, '+', '') AS num, MAX(m.creation) AS last_in
		FROM `tabWhatsApp Message` m
		WHERE m.type = 'Incoming' AND m.creation >= %s AND IFNULL(m.content_type, '') != 'reaction'
		GROUP BY num
		""",
		(since,),
		as_dict=True,
	)
	out = []
	for r in rows:
		last_human = frappe.db.sql(
			f"""SELECT MAX(creation) FROM `tabWhatsApp Message`
			WHERE type = 'Outgoing' AND REPLACE(`to`, '+', '') = %s
			AND owner NOT IN ({", ".join(["%s"] * len(SYSTEM_USERS))})""",
			(r.num, *SYSTEM_USERS),
		)[0][0]
		if last_human and get_datetime(last_human) >= get_datetime(r.last_in):
			continue
		first = frappe.db.sql(
			"""SELECT name, creation FROM `tabWhatsApp Message`
			WHERE type = 'Incoming' AND REPLACE(`from`, '+', '') = %s AND creation > %s
			ORDER BY creation ASC LIMIT 1""",
			(r.num, last_human or since),
			as_dict=True,
		)
		if not first:
			continue
		from butler_whatsapp.inbox import _contact

		contact = _contact(r.num)
		c = frappe.db.get_value(CONTACT, contact, ["butler_status", "butler_assigned_to"], as_dict=True) \
			if contact else None
		if c and c.butler_status == "Closed":
			continue
		out.append(frappe._dict(
			num=r.num, contact=contact, assigned_to=c.butler_assigned_to if c else None,
			first_message=first[0].name, waiting_since=get_datetime(first[0].creation),
		))
	return out


def _alert(row, minutes: int, notify_supervisors: bool):
	from butler_whatsapp.compliance import notify_users
	from butler_whatsapp.inbox import _conversation_title

	title = _conversation_title(row.num)
	users = set()
	if row.assigned_to:
		users.add(row.assigned_to)
	if notify_supervisors or not row.assigned_to:
		users |= set(access.supervisors())
	notify_users(users, f"⏰ {title} ينتظر الرد منذ {minutes} دقيقة", f"/desk/whatsapp-inbox?number={row.num}",
		document=(CONTACT, row.contact) if row.contact else None)
