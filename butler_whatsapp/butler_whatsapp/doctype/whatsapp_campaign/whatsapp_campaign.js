// Copyright (c) 2026, Butler and contributors
// WhatsApp Campaign: find recipients (with the excluded ones and why), start / pause / cancel, live counts.
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

const BWA_CMP = "butler_whatsapp.campaigns";

frappe.ui.form.on("WhatsApp Campaign", {
	setup(frm) {
		frm.set_query("template", () => ({ query: `${BWA_CMP}.marketing_templates` }));
	},

	refresh(frm) {
		bwa_template_preview(frm);
		const st = frm.doc.status;
		const editable = ["Draft", "Paused"].includes(st);
		frm.set_intro("");
		if (frm.is_new()) {
			frm.set_intro(__w("Choose the template and who receives it, save, then press \"Find recipients\"."), "blue");
			return;
		}
		if (editable) {
			frm.add_custom_button(__w("Find recipients"), async () => {
				if (frm.is_dirty()) await frm.save();
				const r = await frappe.call({ method: `${BWA_CMP}.build_recipients`, args: { name: frm.doc.name },
					freeze: true, freeze_message: __w("Checking the numbers...") });
				await frm.reload_doc();
				bwa_show_summary(r.message);
			});
		}
		if (editable && frm.doc.eligible) {
			frm.add_custom_button(st === "Paused" ? __w("Resume") : __w("Start"), () => {
				const when = frm.doc.send_at ? __w("at {0}", [frappe.datetime.str_to_user(frm.doc.send_at)]) : __w("now");
				frappe.confirm(__w("Send the campaign to {0} number(s) {1}?", [bwa_pending(frm), when]) +
					(frm.doc.estimated_cost ? `<br>${__w("Estimated cost")}: ${format_number(frm.doc.estimated_cost, null, 2)}` : ""),
					async () => {
						await frappe.xcall(`${BWA_CMP}.start`, { name: frm.doc.name });
						frm.reload_doc();
					});
			}).addClass("btn-primary");
		}
		if (["Sending", "Scheduled"].includes(st)) {
			frm.add_custom_button(__w("Pause"), async () => {
				await frappe.xcall(`${BWA_CMP}.pause`, { name: frm.doc.name });
				frm.reload_doc();
			});
			frm.set_intro(st === "Sending" ? __w("Sending... the numbers update by themselves.") :
				__w("Scheduled for {0}", [frappe.datetime.str_to_user(frm.doc.send_at)]), "blue");
			clearTimeout(frm.__bwa_timer);
			frm.__bwa_timer = setTimeout(() => frm.doc && frm.doc.name && !frm.is_dirty() && frm.reload_doc(), 15000);
		}
		if (!["Completed", "Cancelled"].includes(st)) {
			frm.add_custom_button(__w("Cancel campaign"), () => frappe.confirm(__w("Cancel this campaign? Messages already sent stay sent."), async () => {
				await frappe.xcall(`${BWA_CMP}.cancel`, { name: frm.doc.name });
				frm.reload_doc();
			}), __w("More"));
		}
		if (editable && (frm.doc.recipients || []).length) {
			const act = (never) => async () => {
				const rows = frm.fields_dict.recipients.grid.get_selected_children().map((r) => r.name);
				if (!rows.length) return frappe.msgprint(__w("Tick the numbers in the table first."));
				await frappe.xcall(`${BWA_CMP}.remove_recipients`, { name: frm.doc.name, rows, never_again: never ? 1 : 0 });
				frm.reload_doc();
			};
			frm.add_custom_button(__w("Remove selected numbers"), act(false), __w("Recipients"));
			frm.add_custom_button(__w("Never send to selected numbers"), act(true), __w("Recipients"));
		}
		if (frm.doc.last_error) frm.dashboard.set_headline_alert(frappe.utils.escape_html(frm.doc.last_error), "orange");
		frm.fields_dict.recipients.grid.cannot_add_rows = true;
	},

	template(frm) {
		bwa_template_preview(frm);
	},
});

function bwa_pending(frm) {
	return (frm.doc.recipients || []).filter((r) => r.status === "Pending").length;
}

function bwa_show_summary(s) {
	if (!s) return;
	const reasons = Object.entries(s.reasons || {})
		.map(([k, v]) => `<li>${frappe.utils.escape_html(__w(k))}: <b>${v}</b></li>`).join("");
	frappe.msgprint({
		title: __w("Recipients"),
		indicator: s.eligible ? "green" : "orange",
		message: `${__w("Will receive")}: <b>${s.eligible}</b><br>${__w("Excluded")}: <b>${s.excluded}</b>
			${reasons ? `<ul style="margin-top:6px">${reasons}</ul>` : ""}
			${s.eligible ? "" : `<div class="text-muted">${__w("Nobody has a recorded consent yet. Collect consents first (WhatsApp Inbox Settings → Collecting Consent).")}</div>`}
			${s.estimated_cost ? `<div>${__w("Estimated cost")}: <b>${format_number(s.estimated_cost, null, 2)} ${frappe.utils.escape_html(s.currency || "")}</b></div>` : ""}`,
	});
}

async function bwa_template_preview(frm) {
	const $w = frm.get_field("template_preview").$wrapper;
	if (!frm.doc.template) return $w.html("");
	const p = await frappe.xcall(`${BWA_CMP}.template_preview`, { template: frm.doc.template });
	const esc = frappe.utils.escape_html;
	$w.html(`<div style="background:#e7fbe0;border-radius:8px;padding:10px;white-space:pre-wrap;max-width:460px">${esc(p.text || "")}
		${p.footer ? `<div class="text-muted small" style="margin-top:6px">${esc(p.footer)}</div>` : ""}</div>
		${p.problem ? `<div class="text-danger small" style="margin-top:6px">⚠ ${esc(p.problem)}</div>` : ""}
		${p.variables ? `<div class="text-muted small">${__w("The template has {0} value(s): write one per line below.", [p.variables])}</div>` : ""}`);
}
