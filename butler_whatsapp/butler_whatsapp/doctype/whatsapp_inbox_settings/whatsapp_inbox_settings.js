// Copyright (c) 2026, Butler and contributors
// WhatsApp Inbox Settings: health refresh, webhook signature status, shortcuts.

const BWA_QUALITY = { GREEN: "🟢 عالية", YELLOW: "🟡 متوسطة", RED: "🔴 منخفضة", NA: "غير متاح", UNKNOWN: "غير معروف" };

frappe.ui.form.on("WhatsApp Inbox Settings", {
	async refresh(frm) {
		frm.add_custom_button("تحديث جودة الرقم والقوالب الآن", async () => {
			await frappe.call({ method: "butler_whatsapp.compliance.refresh_health", freeze: true,
				freeze_message: "جارٍ الاتصال بـMeta..." });
			frm.reload_doc();
		});
		frm.add_custom_button("مسودات القوالب", () => frappe.set_route("List", "WhatsApp Template Draft"));
		frm.add_custom_button("تقرير الأداء", () => frappe.set_route("query-report", "WhatsApp Inbox Performance"));

		if (frm.doc.quality_rating) {
			frm.set_df_property("quality_rating", "description", BWA_QUALITY[frm.doc.quality_rating] || "");
		}
		let st = null;
		try {
			st = await frappe.xcall("butler_whatsapp.compliance.signature_status");
		} catch (e) { /* no permission */ }
		const html = !st
			? `<div class="text-muted small">لم يصل طلب Webhook منذ تفعيل الفحص (أو الفحص معطّل).</div>`
			: `<div class="small"><span class="indicator-pill ${st.ok ? "green" : "red"}">${frappe.utils.escape_html(st.text)}</span>
				<span class="text-muted"> — ${frappe.datetime.str_to_user(st.at)}</span></div>`;
		frm.get_field("signature_status").$wrapper.html(html);
	},
});
