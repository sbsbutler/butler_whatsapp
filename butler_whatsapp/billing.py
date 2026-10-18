"""What Meta charges for WhatsApp messages.

Daily sync from the WhatsApp Business Account's `pricing_analytics`
(Graph API): messages and approximate cost per day, pricing category
(MARKETING, UTILITY, AUTHENTICATION, SERVICE ...) and pricing type
(REGULAR = charged, FREE_CUSTOMER_SERVICE / FREE_ENTRY_POINT = free).
Meta's numbers are approximate; the invoice in Meta Business Manager is final.
COST is not returned when the account is billed through a partner's credit line.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import frappe
import requests
from frappe.utils import flt, getdate, now_datetime

from butler_whatsapp import access

COST = "WhatsApp Cost"
SETTINGS = "WhatsApp Inbox Settings"
DAYS = 45
_TIMEOUT = 30


def _unix(d: datetime) -> int:
	return int(d.replace(tzinfo=timezone.utc).timestamp())


def analytics_field(start: datetime, end: datetime) -> str:
	return (f"pricing_analytics.start({_unix(start)}).end({_unix(end)}).granularity(DAILY)"
		".dimensions(PRICING_CATEGORY,PRICING_TYPE)")


def parse_points(data: dict) -> list[dict]:
	"""Flatten Meta's response into rows {date, pricing_category, pricing_type, volume, cost}."""
	rows: dict[tuple, dict] = {}
	for group in ((data.get("pricing_analytics") or {}).get("data") or []):
		for p in group.get("data_points") or []:
			day = datetime.fromtimestamp(int(p.get("start") or 0), tz=timezone.utc).date()
			key = (day, p.get("pricing_category") or "", p.get("pricing_type") or "")
			row = rows.setdefault(key, {"date": day, "pricing_category": key[1], "pricing_type": key[2],
				"volume": 0, "cost": 0.0})
			row["volume"] += int(p.get("volume") or 0)
			row["cost"] += flt(p.get("cost"))
	return sorted(rows.values(), key=lambda r: (r["date"], r["pricing_category"], r["pricing_type"]))


def _get(account, fields: str) -> dict:
	url = f"{account.url.rstrip('/')}/{account.version}/{account.business_id}"
	resp = requests.get(url, params={"fields": fields},
		headers={"Authorization": f"Bearer {account.get_password('token')}"}, timeout=_TIMEOUT)
	try:
		data = resp.json()
	except ValueError:
		data = {}
	if resp.status_code >= 400 or "error" in data:
		err = data.get("error") or {}
		raise Exception(err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}")
	return data


def sync_costs(days: int = DAYS) -> dict:
	"""Scheduler (daily) and the "Refresh from Meta" button."""
	name = frappe.db.get_value("WhatsApp Account", {"is_default_outgoing": 1, "status": "Active"}, "name")
	if not name or not frappe.db.table_exists(COST):
		return {"rows": 0}
	account = frappe.get_doc("WhatsApp Account", name)
	end = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
	start = (end - timedelta(days=days)).replace(hour=0)
	try:
		try:
			data = _get(account, "currency," + analytics_field(start, end))
		except Exception:
			data = _get(account, analytics_field(start, end))
		rows = parse_points(data)
		currency = data.get("currency") or ""
		frappe.db.delete(COST, {"date": [">=", start.date()]})
		for r in rows:
			frappe.get_doc({"doctype": COST, **r, "currency": currency}).db_insert()
		_set_settings({"billing_currency": currency, "billing_synced_at": now_datetime(), "billing_error": "",
			"billing_month_cost": month_cost()})
		frappe.db.commit()
		return {"rows": len(rows), "currency": currency}
	except Exception as e:
		frappe.db.rollback()
		_set_settings({"billing_synced_at": now_datetime(), "billing_error": str(e)[:500]})
		frappe.db.commit()
		return {"rows": 0, "error": str(e)}


def _set_settings(values: dict):
	meta = frappe.get_meta(SETTINGS)
	for field, value in values.items():
		if meta.has_field(field):
			frappe.db.set_single_value(SETTINGS, field, value)
	frappe.clear_document_cache(SETTINGS, SETTINGS)


def month_cost(day=None) -> float:
	day = getdate(day)
	first = day.replace(day=1)
	value = frappe.db.sql(f"SELECT SUM(cost) FROM `tab{COST}` WHERE date >= %s AND date <= %s", (first, day))
	return round(flt(value[0][0] if value else 0), 2)


@frappe.whitelist(methods=["POST"])
def refresh() -> dict:
	frappe.only_for("System Manager")
	return sync_costs()


@frappe.whitelist()
def overview() -> dict:
	"""Small summary for the settings page."""
	frappe.only_for("System Manager")
	s = access.settings()
	today = getdate()
	return {
		"currency": s.get("billing_currency") or "",
		"this_month": month_cost(today),
		"last_month": month_cost(today.replace(day=1) - timedelta(days=1)),
		"synced_at": str(s.get("billing_synced_at") or ""),
		"error": s.get("billing_error") or "",
	}
