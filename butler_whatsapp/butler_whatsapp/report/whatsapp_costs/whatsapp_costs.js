// Copyright (c) 2026, Butler and contributors
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.query_reports["WhatsApp Costs"] = {
	filters: [
		{ fieldname: "from_date", label: __w("From Date"), fieldtype: "Date", reqd: 1,
			default: frappe.datetime.month_start() },
		{ fieldname: "to_date", label: __w("To Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today() },
		{ fieldname: "group_by", label: __w("Group by"), fieldtype: "Select", default: "Month",
			options: [{ value: "Day", label: __w("Day") }, { value: "Month", label: __w("Month") }] },
		{ fieldname: "only_paid", label: __w("Charged messages only"), fieldtype: "Check", default: 0 },
	],
	onload(report) {
		report.page.add_inner_button(__w("Refresh from Meta"), async () => {
			const r = await frappe.xcall("butler_whatsapp.billing.refresh");
			if (r.error) frappe.msgprint({ title: __w("Meta error"), indicator: "red", message: frappe.utils.escape_html(r.error) });
			else frappe.show_alert({ indicator: "green", message: __w("Updated from Meta") });
			report.refresh();
		});
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "pricing_category" || column.fieldname === "pricing_type") return __w(data[column.fieldname] || "") || value;
		return value;
	},
};
