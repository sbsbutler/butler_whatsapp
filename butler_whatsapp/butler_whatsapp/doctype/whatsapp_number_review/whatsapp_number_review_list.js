// Copyright (c) 2026, Butler and contributors
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.listview_settings["WhatsApp Number Review"] = {
	add_fields: ["decision", "issue"],
	get_indicator(doc) {
		const colors = { Pending: "orange", Add: "green", "Use this name": "blue", "Keep existing": "gray", Ignore: "gray" };
		return [__w(doc.decision), colors[doc.decision] || "gray", `decision,=,${doc.decision}`];
	},
	onload(listview) {
		for (const decision of ["Add", "Use this name", "Keep existing", "Ignore"]) {
			listview.page.add_actions_menu_item(__w(decision), async () => {
				const names = listview.get_checked_items(true);
				if (!names.length) return;
				const done = await frappe.xcall("butler_whatsapp.numbers.decide", { names, decision });
				frappe.show_alert({ indicator: "green", message: __w("{0} number(s) done", [done]) });
				listview.refresh();
			}, false);
		}
	},
};
