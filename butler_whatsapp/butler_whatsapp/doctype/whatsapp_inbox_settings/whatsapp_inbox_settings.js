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

		frm.add_custom_button(__w("Campaigns"), () => frappe.set_route("List", "WhatsApp Campaign"), __w("Marketing"));
		frm.add_custom_button(__w("Import numbers"), () => frappe.new_doc("WhatsApp Number Import"), __w("Marketing"));
		frm.add_custom_button(__w("Numbers to review"), () => frappe.set_route("List", "WhatsApp Number Review", { decision: "Pending" }), __w("Marketing"));
		frm.add_custom_button(__w("Meta Costs"), () => frappe.set_route("query-report", "WhatsApp Costs"), __w("Marketing"));
		frm.set_query("consent_welcome_template", () => ({ filters: { status: "APPROVED" } }));
		bwa_render_consent_links(frm);
		bwa_render_billing(frm);
		frm.get_field("campaign_rules").$wrapper.html(`<div class="small" style="line-height:2">
			<b>${__w("Rules applied to every campaign (Meta policy):")}</b><br>
			✔ ${__w("Only approved MARKETING templates that tell the customer how to stop.")}<br>
			✔ ${__w("Only numbers with a recorded consent; opted-out numbers and the never-send list are excluded.")}<br>
			✔ ${__w("Consent is checked again right before each message.")}<br>
			✔ ${__w("Slow sending, the daily messaging limit of the number is respected, and sending pauses if the quality rating turns red.")}
		</div>`);
	},
});

async function bwa_render_consent_links(frm) {
	const $w = frm.get_field("consent_links_html").$wrapper;
	let r;
	try { r = await frappe.xcall("butler_whatsapp.optin.consent_links"); } catch (e) { return; }
	const esc = frappe.utils.escape_html;
	const box = (title, link, qr, note) => `<div style="border:1px solid var(--border-color);border-radius:8px;padding:10px;margin-bottom:10px">
		<div><b>${title}</b></div>
		${link ? `<div class="small" style="word-break:break-all"><a href="${esc(link)}" target="_blank">${esc(link)}</a>
			<a href="#" class="bwa-copy" data-link="${esc(link)}">📋 ${__w("Copy")}</a></div>` : `<div class="text-muted small">${__w("The WhatsApp number is not known yet (refresh the number quality).")}</div>`}
		${qr ? `<div class="bwa-qr" style="max-width:180px;margin-top:6px">${qr}</div>` : ""}
		<div class="text-muted small">${note}</div></div>`;
	$w.html(
		box(__w("Subscription page"), r.page, r.page_qr, __w("The customer writes the number and ticks the consent box.")) +
		box(__w("WhatsApp link (customer sends \"اشتراك\")"), r.whatsapp, r.whatsapp_qr, __w("Opens WhatsApp with the word ready; sending it records the consent.")) +
		box(__w("Printable paper form (showroom)"), r.paper, "", __w("Print it; record the signed forms from the conversation or import them from Excel.")) +
		`<div class="small" style="border:1px dashed var(--border-color);border-radius:8px;padding:10px">
			<b>${__w("Consent clause for quotations and contracts")}</b>
			<div dir="rtl" style="margin-top:4px">${esc(r.clause_ar)}</div><div dir="ltr" class="text-muted">${esc(r.clause_en)}</div>
			<div class="text-muted">${__w("It is saved as Terms and Conditions \"WhatsApp Consent\". Tick \"Customer agreed in writing\" on the signed document to record the consent.")}</div></div>`);
	$w.find(".bwa-copy").on("click", (e) => {
		e.preventDefault();
		frappe.utils.copy_to_clipboard($(e.currentTarget).attr("data-link"));
	});
	$w.find(".bwa-qr svg").css({ width: "100%", height: "auto" });
}

async function bwa_render_billing(frm) {
	const $w = frm.get_field("billing_overview").$wrapper;
	let o;
	try { o = await frappe.xcall("butler_whatsapp.billing.overview"); } catch (e) { return; }
	const cur = o.currency ? ` ${frappe.utils.escape_html(o.currency)}` : "";
	$w.html(`<div class="small" style="line-height:2">
		<div>${__w("This month")}: <b>${format_number(o.this_month, null, 2)}${cur}</b></div>
		<div>${__w("Last month")}: <b>${format_number(o.last_month, null, 2)}${cur}</b></div>
		<div class="text-muted">${__w("Approximate amounts from Meta; the final invoice is in Meta Business Manager.")}</div>
		<button class="btn btn-xs btn-default bwa-bill-refresh">${__w("Refresh from Meta")}</button>
		<button class="btn btn-xs btn-default bwa-bill-report">${__w("Costs report")}</button></div>`);
	$w.find(".bwa-bill-refresh").on("click", async () => {
		const r = await frappe.call({ method: "butler_whatsapp.billing.refresh", freeze: true, freeze_message: __w("Contacting Meta...") });
		if (r.message && r.message.error) frappe.msgprint({ title: __w("Meta error"), indicator: "red", message: frappe.utils.escape_html(r.message.error) });
		frm.reload_doc();
	});
	$w.find(".bwa-bill-report").on("click", () => frappe.set_route("query-report", "WhatsApp Costs"));
}
