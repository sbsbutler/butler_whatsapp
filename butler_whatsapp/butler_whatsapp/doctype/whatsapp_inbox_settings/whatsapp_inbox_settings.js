// Copyright (c) 2026, Butler and contributors
// WhatsApp Inbox Settings: health refresh, webhook signature status, shortcuts.
// translations in the "WhatsApp" context (translations/ar.csv), falling back to Frappe's own
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

const BWA_QUALITY = { GREEN: "🟢 " + __w("High"), YELLOW: "🟡 " + __w("Medium"), RED: "🔴 " + __w("Low"), NA: __w("Not available"), UNKNOWN: __w("Unknown") };

frappe.ui.form.on("WhatsApp Inbox Settings", {
	async refresh(frm) {
		frm.add_custom_button(__w("Refresh number quality and templates now"), async () => {
			await frappe.call({ method: "butler_whatsapp.compliance.refresh_health", freeze: true,
				freeze_message: __w("Contacting Meta...") });
			frm.reload_doc();
		});
		frm.add_custom_button(__w("Teams"), () => frappe.set_route("List", "WhatsApp Team"));
		frm.add_custom_button(__w("Template Drafts"), () => frappe.set_route("List", "WhatsApp Template Draft"));
		frm.add_custom_button(__w("Inbox Performance"), () => frappe.set_route("query-report", "WhatsApp Inbox Performance"));

		if (frm.doc.quality_rating) {
			frm.set_df_property("quality_rating", "description", BWA_QUALITY[frm.doc.quality_rating] || "");
		}
		let st = null;
		try {
			st = await frappe.xcall("butler_whatsapp.compliance.signature_status");
		} catch (e) { /* no permission */ }
		const html = !st
			? `<div class="text-muted small">${__w("No webhook request has arrived since the check was enabled (or the check is off).")}</div>`
			: `<div class="small"><span class="indicator-pill ${st.ok ? "green" : "red"}">${frappe.utils.escape_html(__w(st.text))}</span>
				<span class="text-muted"> — ${frappe.datetime.str_to_user(st.at)}</span></div>`;
		frm.get_field("signature_status").$wrapper.html(html);
	},
});
