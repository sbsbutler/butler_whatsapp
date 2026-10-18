// Copyright (c) 2026, Butler and contributors
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.ui.form.on("WhatsApp Number Import", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.status === "Queued") {
			frm.set_intro(__w("Importing in the background... this page refreshes by itself."), "blue");
			setTimeout(() => frm.reload_doc(), 4000);
		}
		if (!frm.is_new() && ["Draft", "Failed", "Done"].includes(frm.doc.status)) {
			const label = frm.doc.status === "Done" ? __w("Import again") : __w("Start import");
			frm.add_custom_button(label, async () => {
				if (frm.is_dirty()) await frm.save();
				await frappe.xcall("butler_whatsapp.numbers.run_import", { name: frm.doc.name });
				frm.reload_doc();
			}).addClass("btn-primary");
		}
		if (frm.doc.to_review) {
			frm.add_custom_button(__w("Numbers to review"), () =>
				frappe.set_route("List", "WhatsApp Number Review", { number_import: frm.doc.name, decision: "Pending" }));
		}
		if (frm.doc.status === "Done") {
			frm.add_custom_button(__w("Campaign to these numbers"), () =>
				frappe.new_doc("WhatsApp Campaign", { number_import: frm.doc.name, to_customers: 1, to_leads: 1, to_others: 1, to_staff: 1 }));
		}
		if (frm.doc.source === "Excel" && !frm.doc.file) {
			frm.set_intro(__w("Attach the Excel / CSV file, then check the column names and save."), "blue");
		}
	},
});
