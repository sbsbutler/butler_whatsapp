// Copyright (c) 2026, Butler and contributors
// WhatsApp Team: add several staff members at once.
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.ui.form.on("WhatsApp Team", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__w("Add members"), () => bwa_add_members(frm)).addClass("btn-primary");
	},
});

async function bwa_add_members(frm) {
	const have = new Set((frm.doc.members || []).map((m) => m.user));
	const users = await frappe.db.get_list("User", {
		filters: { enabled: 1, user_type: "System User", name: ["not in", ["Administrator", "Guest"]] },
		fields: ["name", "full_name"], limit: 500, order_by: "full_name asc",
	});
	const options = users.filter((u) => !have.has(u.name))
		.map((u) => ({ value: u.name, label: u.full_name || u.name, description: u.name }));
	if (!options.length) {
		frappe.msgprint(__w("All staff are already in this team."));
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __w("Add members to {0}", [__w(frm.doc.team_name)]),
		fields: [{ fieldname: "users", fieldtype: "MultiSelectList", label: __w("Staff"), reqd: 1,
			get_data: (txt) => options.filter((o) => !txt || (o.label + " " + o.value).toLowerCase().includes(txt.toLowerCase())) }],
		primary_action_label: __w("Add"),
		primary_action: async (v) => {
			const added = await frappe.xcall("butler_whatsapp.teams.add_members", { team: frm.doc.name, users: v.users });
			d.hide();
			frappe.show_alert({ indicator: "green", message: __w("{0} member(s) added", [added.length]) });
			frm.reload_doc();
		},
	});
	d.show();
}
