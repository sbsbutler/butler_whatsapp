# Copyright (c) 2026, Butler and contributors
"""What Meta charges for WhatsApp messages (synced daily by butler_whatsapp.billing)."""

import frappe
from frappe import _
from frappe.utils import flt

from butler_whatsapp import access


def execute(filters=None):
	filters = frappe._dict(filters or {})
	currency = access.settings().get("billing_currency") or ""
	month = filters.group_by != "Day"
	period = "DATE_FORMAT(date, '%%Y-%%m')" if month else "date"
	conds = ["date BETWEEN %(from_date)s AND %(to_date)s"]
	if filters.only_paid:
		conds.append("pricing_type = 'REGULAR'")
	rows = frappe.db.sql(
		f"""SELECT {period} AS period, pricing_category, pricing_type, SUM(volume) AS volume, SUM(cost) AS cost
		FROM `tabWhatsApp Cost` WHERE {' AND '.join(conds)}
		GROUP BY period, pricing_category, pricing_type ORDER BY period, pricing_category, pricing_type""",
		filters, as_dict=True,
	)
	label_cost = _("Cost") + (f" ({currency})" if currency else "")
	columns = [
		{"fieldname": "period", "label": _("Month") if month else _("Date"), "fieldtype": "Data" if month else "Date",
		 "width": 110},
		{"fieldname": "pricing_category", "label": _("Category"), "fieldtype": "Data", "width": 160},
		{"fieldname": "pricing_type", "label": _("Pricing Type"), "fieldtype": "Data", "width": 190},
		{"fieldname": "volume", "label": _("Messages"), "fieldtype": "Int", "width": 110},
		{"fieldname": "cost", "label": label_cost, "fieldtype": "Float", "precision": 2, "width": 130},
	]
	by_cat = {}
	for r in rows:
		by_cat[r.pricing_category] = by_cat.get(r.pricing_category, 0) + flt(r.cost)
	total = sum(by_cat.values())
	messages = sum(int(r.volume or 0) for r in rows)
	free = sum(int(r.volume or 0) for r in rows if r.pricing_type != "REGULAR")
	summary = [
		{"label": label_cost, "value": round(total, 2), "datatype": "Float", "indicator": "Blue"},
		{"label": _("Messages"), "value": messages, "datatype": "Int"},
		{"label": _("Free messages"), "value": free, "datatype": "Int", "indicator": "Green"},
	]
	chart = None
	if by_cat:
		chart = {"data": {"labels": [_(k or "-") for k in by_cat],
			"datasets": [{"name": label_cost, "values": [round(v, 2) for v in by_cat.values()]}]},
			"type": "pie"}
	s = access.settings()
	message = None
	if s.get("billing_error"):
		message = _("Last sync from Meta failed: {0}").format(frappe.utils.escape_html(s.get("billing_error")))
	elif not rows:
		message = _("No data yet. Press \"Refresh from Meta\". If the cost stays empty, the account may be billed through a partner (Meta does not return the cost then).")
	return columns, rows, message, chart, summary
