// Copyright (c) 2026, Butler and contributors
// WhatsApp Template Draft: live Meta-rule check, preview and "submit to Meta".
// translations in the "WhatsApp" context (translations/ar.csv), falling back to Frappe's own
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

const BWA_DRAFT_FIELDS = ["body", "body_samples", "header_type", "header", "header_sample", "footer", "language",
	"button_type", "button_label", "button_url", "button_phone"];

function bwaRender(text, samples) {
	let out = frappe.utils.escape_html(text || "");
	(samples || []).forEach((s, i) => {
		out = out.replace(new RegExp(`\\{\\{\\s*${i + 1}\\s*\\}\\}`, "g"), `<b>${frappe.utils.escape_html(s)}</b>`);
	});
	return out.replace(/\n/g, "<br>");
}

const bwaCheck = frappe.utils.debounce(async (frm) => {
	const d = frm.doc;
	const samples = (d.body_samples || "").split("\n").map((s) => s.trim()).filter(Boolean);
	let problems = [];
	try {
		problems = await frappe.xcall("butler_whatsapp.butler_whatsapp.doctype.whatsapp_template_draft.whatsapp_template_draft.check_draft", {
			body: d.body || "", body_samples: d.body_samples || "", header_type: d.header_type || "",
			header: d.header || "", header_sample: d.header_sample || "", footer: d.footer || "",
			button_type: d.button_type || "", button_label: d.button_label || "", button_url: d.button_url || "",
			button_phone: d.button_phone || "",
		});
	} catch (e) { /* ignore while typing */ }
	const head = d.header_type === "TEXT" && d.header
		? `<div style="font-weight:600;margin-bottom:6px">${bwaRender(d.header, [d.header_sample || ""])}</div>`
		: d.header_type === "DOCUMENT" ? `<div style="margin-bottom:6px">📄 ${__w("PDF file")}</div>`
		: d.header_type === "IMAGE" ? `<div style="margin-bottom:6px">🖼️ ${__w("Image")}</div>` : "";
	const foot = d.footer ? `<div class="text-muted small" style="margin-top:6px">${frappe.utils.escape_html(d.footer)}</div>` : "";
	const button = d.button_type && d.button_label
		? `<div style="border-top:1px solid rgba(0,0,0,.1);margin-top:8px;padding-top:6px;text-align:center;color:#027eb5">
			${d.button_type === "Call Phone" ? "📞" : "🔗"} ${frappe.utils.escape_html(d.button_label)}</div>` : "";
	const status = problems.length
		? `<div class="alert alert-warning small" style="margin-top:8px"><b>${__w("Fix before sending to Meta:")}</b><br>${problems.map((p) => "• " + frappe.utils.escape_html(p)).join("<br>")}</div>`
		: `<div class="alert alert-success small" style="margin-top:8px">✓ ${__w("Follows Meta's basic rules")}</div>`;
	const dir = (d.language || "").startsWith("ar") ? "rtl" : "ltr";
	frm.get_field("preview_html").$wrapper.html(`
		<div dir="${dir}" style="max-width:420px;background:#d9fdd3;color:#111b21;border-radius:10px;padding:10px 12px;line-height:1.8">
			${head}${bwaRender(d.body, samples)}${foot}${button}
		</div>${status}`);
}, 400);

frappe.ui.form.on("WhatsApp Template Draft", {
	refresh(frm) {
		bwaCheck(frm);
		frm.get_field("help_html").$wrapper.html(`<div class="text-muted small" style="line-height:1.9">
			<b>${__w("How a draft works:")}</b> ${__w("Edit the text, then press \"Submit to Meta for approval\". Meta reviews the template (usually minutes to 24 hours) and it appears in the inbox automatically once approved.")}<br>
			<b>${__w("Meta's rules:")}</b> ${__w("Variables {{1}} {{2}}... are consecutive, the text does not start or end with a variable, and every variable has a realistic sample. UTILITY templates are for an existing transaction with the customer (a quotation they asked for, an invoice, an appointment); any promotion or offer is MARKETING.")}<br>
			<b>${__w("Languages:")}</b> ${__w("Create the same Name at Meta once per language. Customers get the version in their language automatically.")}
		</div>`);
		const sent = !!frm.doc.whatsapp_template;
		frm.set_df_property("body", "read_only", sent ? 1 : 0);
		frm.set_df_property("template_name", "read_only", sent ? 1 : 0);
		if (!frm.is_new() && !sent) {
			frm.add_custom_button(__w("Submit to Meta for approval"), () => {
				frappe.confirm(
					__w("The template will be sent to Meta for review, and its text can then only be changed from the template page. Continue?"),
					async () => {
						if (frm.is_dirty()) await frm.save();
						const r = await frappe.call({
							method: "butler_whatsapp.butler_whatsapp.doctype.whatsapp_template_draft.whatsapp_template_draft.submit_to_meta",
							args: { name: frm.doc.name }, freeze: true, freeze_message: __w("Sending to Meta..."),
						});
						const m = r.message || {};
						if (m.ok) {
							frappe.show_alert({ indicator: "green", message: __w("Submitted for approval ({0})", [m.status || "PENDING"]) });
						} else {
							frappe.msgprint({ title: __w("Meta rejected the request"), indicator: "red", message: frappe.utils.escape_html(m.error || "") });
						}
						frm.reload_doc();
					}
				);
			}).addClass("btn-primary");
		}
		if (sent) {
			frm.add_custom_button(__w("Open the template"), () => frappe.set_route("Form", "WhatsApp Templates", frm.doc.whatsapp_template));
		}
	},
	...Object.fromEntries(BWA_DRAFT_FIELDS.map((f) => [f, (frm) => bwaCheck(frm)])),
});
