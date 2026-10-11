// Copyright (c) 2026, Butler and contributors
// WhatsApp Template Draft: live Meta-rule check, preview and "submit to Meta".

const BWA_DRAFT_FIELDS = ["body", "body_samples", "header_type", "header", "header_sample", "footer"];

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
		});
	} catch (e) { /* ignore while typing */ }
	const head = d.header_type === "TEXT" && d.header
		? `<div style="font-weight:600;margin-bottom:6px">${bwaRender(d.header, [d.header_sample || ""])}</div>`
		: d.header_type === "DOCUMENT" ? `<div style="margin-bottom:6px">📄 ملف PDF</div>`
		: d.header_type === "IMAGE" ? `<div style="margin-bottom:6px">🖼️ صورة</div>` : "";
	const foot = d.footer ? `<div class="text-muted small" style="margin-top:6px">${frappe.utils.escape_html(d.footer)}</div>` : "";
	const status = problems.length
		? `<div class="alert alert-warning small" style="margin-top:8px"><b>ملاحظات قبل الإرسال لـMeta:</b><br>${problems.map((p) => "• " + frappe.utils.escape_html(p)).join("<br>")}</div>`
		: `<div class="alert alert-success small" style="margin-top:8px">✓ يطابق قواعد Meta الأساسية</div>`;
	frm.get_field("preview_html").$wrapper.html(`
		<div dir="rtl" style="max-width:420px;background:#d9fdd3;color:#111b21;border-radius:10px;padding:10px 12px;line-height:1.8">
			${head}${bwaRender(d.body, samples)}${foot}
		</div>${status}`);
}, 400);

frappe.ui.form.on("WhatsApp Template Draft", {
	refresh(frm) {
		bwaCheck(frm);
		const sent = !!frm.doc.whatsapp_template;
		frm.set_df_property("body", "read_only", sent ? 1 : 0);
		frm.set_df_property("template_name", "read_only", sent ? 1 : 0);
		if (!frm.is_new() && !sent) {
			frm.add_custom_button("إرسال إلى Meta للاعتماد", () => {
				frappe.confirm(
					"سيُرسل القالب إلى Meta للمراجعة، ولا يمكن تعديل نصه بعد ذلك إلا من صفحة القالب. متابعة؟",
					async () => {
						if (frm.is_dirty()) await frm.save();
						const r = await frappe.call({
							method: "butler_whatsapp.butler_whatsapp.doctype.whatsapp_template_draft.whatsapp_template_draft.submit_to_meta",
							args: { name: frm.doc.name }, freeze: true, freeze_message: "جارٍ الإرسال إلى Meta...",
						});
						const m = r.message || {};
						if (m.ok) {
							frappe.show_alert({ indicator: "green", message: `أُرسل للاعتماد (${m.status || "PENDING"})` });
						} else {
							frappe.msgprint({ title: "رفضت Meta الطلب", indicator: "red", message: frappe.utils.escape_html(m.error || "") });
						}
						frm.reload_doc();
					}
				);
			}).addClass("btn-primary");
		}
		if (sent) {
			frm.add_custom_button("فتح القالب", () => frappe.set_route("Form", "WhatsApp Templates", frm.doc.whatsapp_template));
		}
	},
	...Object.fromEntries(BWA_DRAFT_FIELDS.map((f) => [f, (frm) => bwaCheck(frm)])),
});
