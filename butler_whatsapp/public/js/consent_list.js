// Copyright (c) 2026, Butler and contributors
// Customer / Lead list: e-mail the selected records a link to agree to WhatsApp messages (Meta opt-in).
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

(function () {
	const add = (doctype) => {
		const prev = frappe.listview_settings[doctype] || {};
		if (prev.__bwa_consent) return;
		prev.__bwa_consent = true;
		const prev_onload = prev.onload;
		frappe.listview_settings[doctype] = Object.assign(prev, {
			onload(listview) {
				prev_onload && prev_onload(listview);
				listview.page.add_actions_menu_item(__w("Ask for WhatsApp consent by e-mail"), () => {
					const names = listview.get_checked_items(true);
					if (!names.length) return frappe.msgprint(__w("Select the records first."));
					frappe.confirm(__w("Send {0} e-mail(s) with a link to agree to Butler's WhatsApp messages? Records that already agreed or asked to stop are skipped.", [names.length]), async () => {
						const r = await frappe.xcall("butler_whatsapp.optin.email_consent_request", { doctype, names });
						frappe.msgprint({
							title: __w("WhatsApp consent"),
							message: `${__w("Sent")}: ${r.sent}<br>${__w("Without e-mail")}: ${r.no_email}<br>${__w("Without mobile number")}: ${r.no_mobile}<br>${__w("Already answered")}: ${r.already}`,
						});
					});
				}, false);
			},
		});
	};
	add("Customer");
	add("Lead");
})();
