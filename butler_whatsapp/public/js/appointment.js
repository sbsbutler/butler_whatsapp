// Copyright (c) 2026, Butler and contributors
// Appointment: send / resend the WhatsApp confirmation of a showroom visit (appointments.py).
// translations in the "WhatsApp" context (translations/ar.csv), falling back to Frappe's own
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.ui.form.on("Appointment", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status === "Closed") return;
		const sent = frm.doc.butler_wa_confirmation_sent;
		frm.add_custom_button(sent ? __w("Resend WhatsApp confirmation") : __w("Send WhatsApp confirmation"), async () => {
			if (frm.is_dirty()) await frm.save();
			await frappe.call({
				method: "butler_whatsapp.appointments.send_confirmation",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __w("Sending..."),
			});
			frappe.show_alert({ indicator: "green", message: __w("WhatsApp confirmation sent") + " ✅" });
			frm.reload_doc();
		}, __w("WhatsApp"));
	},
});
