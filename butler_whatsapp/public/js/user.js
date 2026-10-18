// Copyright (c) 2026, Butler and contributors
// User form: choose the WhatsApp teams of this user.
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.ui.form.on("User", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.user_type !== "System User" || !frappe.user.has_role("System Manager")) return;
		frm.add_custom_button(__w("WhatsApp Teams"), async () => {
			const teams = await frappe.xcall("butler_whatsapp.teams.get_user_teams", { user: frm.doc.name });
			const d = new frappe.ui.Dialog({
				title: __w("WhatsApp Teams of {0}", [frm.doc.full_name || frm.doc.name]),
				fields: teams.map((t) => ({ fieldname: t.team, fieldtype: "Check", label: __w(t.team), default: t.member ? 1 : 0 })),
				primary_action_label: __w("Save"),
				primary_action: async (v) => {
					const chosen = teams.filter((t) => v[t.team]).map((t) => t.team);
					await frappe.xcall("butler_whatsapp.teams.set_user_teams", { user: frm.doc.name, teams: chosen });
					d.hide();
					frappe.show_alert({ indicator: "green", message: __w("Saved") });
				},
			});
			d.show();
		});
	},
});
