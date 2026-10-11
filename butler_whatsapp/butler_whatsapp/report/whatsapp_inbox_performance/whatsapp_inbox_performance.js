// Copyright (c) 2026, Butler and contributors

frappe.query_reports["WhatsApp Inbox Performance"] = {
	filters: [
		{ fieldname: "from_date", label: "من تاريخ", fieldtype: "Date", default: frappe.datetime.add_days(frappe.datetime.get_today(), -7), reqd: 1 },
		{ fieldname: "to_date", label: "إلى تاريخ", fieldtype: "Date", default: frappe.datetime.get_today(), reqd: 1 },
	],
};
