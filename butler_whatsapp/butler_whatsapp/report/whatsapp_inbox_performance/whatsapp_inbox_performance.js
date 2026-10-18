// Copyright (c) 2026, Butler and contributors
// translations in the "WhatsApp" context (translations/ar.csv), falling back to Frappe's own
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.query_reports["WhatsApp Inbox Performance"] = {
	filters: [
		{ fieldname: "from_date", label: __w("From Date"), fieldtype: "Date", default: frappe.datetime.add_days(frappe.datetime.get_today(), -7), reqd: 1 },
		{ fieldname: "to_date", label: __w("To Date"), fieldtype: "Date", default: frappe.datetime.get_today(), reqd: 1 },
		{ fieldname: "team", label: __w("Team"), fieldtype: "Link", options: "WhatsApp Team" },
	],
};
