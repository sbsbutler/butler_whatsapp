// Copyright (c) 2026, Butler and contributors
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.ui.form.on("WhatsApp Number Review", {
	refresh(frm) {
		const help = {
			"Repeated in the import": __w("The same number is written more than once with different names. Choose the right name (Use this name) or Ignore."),
			"Number exists with another name": __w("This number is already saved with another name. Use this name to rename it, or Keep existing."),
			"Invalid number": __w("Correct the number (with country code, e.g. 9665XXXXXXXX) then choose Add, or Ignore."),
			"No country code": __w("Write the number with its country code (e.g. 9665XXXXXXXX for Saudi Arabia) then choose Add, or Ignore."),
		};
		frm.set_intro(help[frm.doc.issue] || "", "blue");
		frm.set_df_property("number", "read_only", frm.doc.decision !== "Pending");
		if (frm.doc.decision === "Pending") {
			for (const decision of ["Add", "Use this name", "Keep existing", "Ignore"]) {
				frm.add_custom_button(__w(decision), async () => {
					frm.set_value("decision", decision);
					await frm.save();
				}, __w("Decide"));
			}
		}
	},
});
