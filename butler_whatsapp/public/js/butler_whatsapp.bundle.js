// Butler WhatsApp - helpers for Desk (loaded on every Desk page via app_include_js)
//  1. A floating button (with unread badge) that opens the WhatsApp inbox page
//     (butler_whatsapp/butler_whatsapp/page/whatsapp_inbox).
//     Draggable (snaps to the nearest side), position kept per device; can be hidden.
//  2. "Send via WhatsApp" on quotations, orders and invoices (PDF to customer);
//     "WhatsApp" on Customer / Lead / Contact opens their conversation.
//  3. Web Push: notifications on phones (iOS home-screen app / Android) and desktops.

(() => {
	// ------------------------------------------------------------ legacy chat bubble
	// The whatsapp_chat app mounts its own bubble (.chat-app) with no on/off setting.
	// It ignores inbox assignment, so it is hidden for everyone; the app itself must
	// stay installed because it owns the "WhatsApp Contact" DocType.
	(function hideLegacyChat() {
		if (document.getElementById("butler-wa-hide-legacy")) return;
		const style = document.createElement("style");
		style.id = "butler-wa-hide-legacy";
		style.textContent = ".chat-app { display: none !important; }";
		(document.head || document.documentElement).appendChild(style);
	})();

	// ------------------------------------------------------------ inbox launcher
	const LAUNCHER_ID = "butler-wa-launcher";
	const INBOX = "whatsapp-inbox";
	const INBOX_API = "butler_whatsapp.inbox";

	const canUse = () => frappe.session && frappe.session.user !== "Guest" &&
		frappe.model && frappe.model.can_read && frappe.model.can_read("WhatsApp Message");
	const onInbox = () => (frappe.get_route_str ? frappe.get_route_str() || "" : "").endsWith(INBOX);

	function setBadge(n) {
		const badge = document.getElementById(`${LAUNCHER_ID}-badge`);
		if (!badge) return;
		n = Math.max(0, n || 0);
		badge.textContent = n > 99 ? "99+" : String(n);
		badge.style.display = n ? "flex" : "none";
	}

	const refreshBadge = frappe.utils.debounce(async () => {
		if (!canUse()) return;
		try {
			setBadge(await frappe.xcall(`${INBOX_API}.get_unread_total`));
		} catch (e) { /* offline */ }
	}, 600);

	function openInbox(number, link) {
		const num = String(number || "").replace(/[^0-9]/g, "");
		if (num) frappe.route_options = { number: num, ...(link || {}) };
		frappe.set_route(INBOX);
	}

	// Launcher position / visibility are per user and per device (localStorage).
	const LS_POS = "butler_wa_launcher_pos";
	const LS_HIDDEN = "butler_wa_launcher_hidden";
	const SIZE = 52, MARGIN = 12;

	const launcherAllowed = () => {
		const b = frappe.boot && frappe.boot.butler_wa;
		return !b || b.show_launcher !== 0;
	};

	function readPos() {
		try {
			return JSON.parse(lsGet(LS_POS) || "null");
		} catch (e) {
			return null;
		}
	}

	function applyPos(btn, pos) {
		pos = pos || { side: "left", bottom: 20 };
		const maxBottom = window.innerHeight - SIZE - MARGIN;
		const bottom = Math.min(Math.max(MARGIN, pos.bottom), Math.max(MARGIN, maxBottom));
		btn.style.left = pos.side === "left" ? MARGIN + 8 + "px" : "auto";
		btn.style.right = pos.side === "right" ? MARGIN + 8 + "px" : "auto";
		btn.style.bottom = bottom + "px";
		btn.style.top = "auto";
	}

	function makeDraggable(btn) {
		let start = null, moved = false;
		btn.addEventListener("pointerdown", (e) => {
			if (e.button !== 0) return;
			start = { x: e.clientX, y: e.clientY };
			moved = false;
			btn.setPointerCapture(e.pointerId);
		});
		btn.addEventListener("pointermove", (e) => {
			if (!start) return;
			if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 6) return;
			moved = true;
			btn.style.transition = "none";
			btn.style.left = Math.min(Math.max(0, e.clientX - SIZE / 2), window.innerWidth - SIZE) + "px";
			btn.style.right = "auto";
			btn.style.top = Math.min(Math.max(0, e.clientY - SIZE / 2), window.innerHeight - SIZE) + "px";
			btn.style.bottom = "auto";
		});
		btn.addEventListener("pointerup", (e) => {
			if (!start) return;
			start = null;
			if (!moved) return;
			// snap to the nearest side, keep the height
			const side = e.clientX < window.innerWidth / 2 ? "left" : "right";
			const pos = { side, bottom: Math.round(window.innerHeight - e.clientY - SIZE / 2) };
			btn.style.transition = "left .2s, right .2s";
			applyPos(btn, pos);
			lsSet(LS_POS, JSON.stringify(pos));
			btn.dataset.dragged = "1";
			setTimeout(() => delete btn.dataset.dragged, 50);
		});
	}

	function addLauncher() {
		if (document.getElementById(LAUNCHER_ID)) return;
		if (!launcherAllowed() || lsGet(LS_HIDDEN) === "1") return;
		const btn = document.createElement("button");
		btn.id = LAUNCHER_ID;
		btn.type = "button";
		btn.title = "واتساب — اسحبه لتغيير مكانه";
		btn.innerHTML = `<svg viewBox="0 0 24 24" width="26" height="26" fill="#fff" aria-hidden="true">
			<path d="M12 3C6.5 3 2 6.8 2 11.5c0 2.6 1.4 4.9 3.6 6.5L5 22l4.4-2.3c.8.2 1.7.3 2.6.3 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/></svg>
			<span id="${LAUNCHER_ID}-badge" style="display:none;position:absolute;top:-4px;right:-4px;min-width:20px;height:20px;
				padding:0 5px;border-radius:10px;background:#e53935;color:#fff;font-size:11px;font-weight:600;
				align-items:center;justify-content:center;"></span>`;
		Object.assign(btn.style, {
			position: "fixed", zIndex: 1031, touchAction: "none",
			width: SIZE + "px", height: SIZE + "px", borderRadius: "50%", border: "none",
			background: "#25D366", boxShadow: "0 4px 12px rgba(0,0,0,.25)", cursor: "pointer",
			display: "flex", alignItems: "center", justifyContent: "center",
		});
		applyPos(btn, readPos());
		makeDraggable(btn);
		btn.addEventListener("click", (e) => {
			e.stopPropagation();
			if (btn.dataset.dragged) return;
			openInbox();
			if (e.isTrusted && shouldOfferPush()) setTimeout(showPushDialog, 600);
		});
		document.body.appendChild(btn);
		window.addEventListener("resize", frappe.utils.debounce(() => applyPos(btn, readPos()), 200));
		refreshBadge();
	}

	function toggleLauncher() {
		const el = document.getElementById(LAUNCHER_ID);
		if (el) {
			el.remove();
			lsSet(LS_HIDDEN, "1");
			frappe.show_alert("أُخفي الزر العائم على هذا الجهاز — يمكنك إظهاره من قائمة صفحة واتساب");
		} else {
			lsSet(LS_HIDDEN, "0");
			addLauncher();
			if (!document.getElementById(LAUNCHER_ID)) frappe.msgprint("الزر العائم معطّل من إعدادات صندوق واتساب.");
		}
	}

	function resetLauncher() {
		lsSet(LS_POS, "");
		lsSet(LS_HIDDEN, "0");
		const el = document.getElementById(LAUNCHER_ID);
		if (el) applyPos(el, null);
		else addLauncher();
	}

	function notifyDesktop(data) {
		if (!("Notification" in window) || Notification.permission !== "granted") return;
		if (!document.hidden) return; // the in-page alert is enough when the tab is visible
		if (pushEnabledHere()) return; // the service worker shows it (avoid duplicates)
		const n = new Notification(`WhatsApp: ${data.display}`, {
			body: data.content || "",
			tag: `butler-wa-${data.from}`,
			renotify: true,
		});
		n.onclick = () => {
			window.focus();
			openInbox(data.from);
			n.close();
		};
	}

	function onIncoming(data) {
		refreshBadge();
		notifyDesktop(data);
		if (!document.hidden && !onInbox()) {
			frappe.show_alert({
				message: `<a href="/desk/${INBOX}?number=${encodeURIComponent(String(data.from || "").replace(/[^0-9]/g, ""))}">
					💬 <b>${frappe.utils.escape_html(data.display)}</b><br>${frappe.utils.escape_html(data.content || "")}</a>`,
				indicator: "green",
			}, 8);
			frappe.utils.play_sound("chat-message-receive");
		}
	}

	function numberFromUrl(url) {
		try {
			return new URL(url, location.origin).searchParams.get("number");
		} catch (e) {
			return null;
		}
	}

	$(() => {
		addAppHeadTags();
		if (!canUse()) return;
		addLauncher();
		refreshBadge();
		frappe.realtime.on("butler_wa_incoming", onIncoming);
		frappe.realtime.on("butler_wa_update", () => refreshBadge());
		frappe.realtime.on("butler_wa_assigned", (d) => {
			refreshBadge();
			frappe.show_alert({
				message: `<a href="/desk/${INBOX}?number=${encodeURIComponent(d.number)}">📌 أُسندت إليك محادثة:
					<b>${frappe.utils.escape_html(d.title)}</b>${d.by ? " — " + frappe.utils.escape_html(d.by) : ""}</a>`,
				indicator: "blue",
			}, 12);
		});
		refreshPushSubscription();
		const params = new URLSearchParams(location.search);
		if (params.has("butler_wa")) openInbox(params.get("number"));
		if (params.has("butler_wa_push")) setTimeout(showPushDialog, 1500);
		if ("serviceWorker" in navigator) {
			navigator.serviceWorker.addEventListener("message", (ev) => {
				if (ev.data && ev.data.type === "butler_wa_open") openInbox(numberFromUrl(ev.data.url));
			});
		}
	});

	// ------------------------------------------------------------ web push
	const PUSH = "butler_whatsapp.push";
	const SW_URL = `/api/method/${PUSH}.service_worker`;
	const LS_ENABLED = "butler_wa_push_enabled";
	const LS_SNOOZE = "butler_wa_push_snooze_until";

	const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
	const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } };

	const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent) ||
		(navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
	const isStandalone = () => window.navigator.standalone === true ||
		window.matchMedia("(display-mode: standalone)").matches;
	const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
	const pushEnabledHere = () => lsGet(LS_ENABLED) === "1" && "Notification" in window &&
		Notification.permission === "granted";

	function shouldOfferPush() {
		if (pushEnabledHere()) return false;
		if ("Notification" in window && Notification.permission === "denied") return false;
		const until = parseInt(lsGet(LS_SNOOZE) || "0", 10);
		if (Date.now() < until) return false;
		return pushSupported() || isIOS();
	}

	function addAppHeadTags() {
		const add = (tag, attrs) => {
			const sel = Object.entries(attrs).filter(([k]) => k !== "content" && k !== "href")
				.map(([k, v]) => `[${k}="${v}"]`).join("");
			if (document.head.querySelector(`${tag}${sel}`)) return;
			const el = document.createElement(tag);
			Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
			document.head.appendChild(el);
		};
		add("link", { rel: "manifest", href: `/api/method/${PUSH}.manifest` });
		add("link", { rel: "apple-touch-icon", href: "/files/butler-app-icon-192.png" });
		add("meta", { name: "apple-mobile-web-app-capable", content: "yes" });
		add("meta", { name: "mobile-web-app-capable", content: "yes" });
		add("meta", { name: "apple-mobile-web-app-title", content: "Butler" });
	}

	function keyToBytes(base64url) {
		const pad = "=".repeat((4 - (base64url.length % 4)) % 4);
		const raw = atob((base64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
		return Uint8Array.from(raw, (c) => c.charCodeAt(0));
	}

	function deviceLabel() {
		const ua = navigator.userAgent;
		const os = isIOS() ? "iPhone/iPad" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" :
			/Mac/.test(ua) ? "Mac" : "Other";
		const br = /Edg\//.test(ua) ? "Edge" : /CriOS|Chrome\//.test(ua) ? "Chrome" : /Firefox|FxiOS/.test(ua) ? "Firefox" :
			/Safari\//.test(ua) ? "Safari" : "Browser";
		return `${os} - ${br}${isStandalone() ? " (app)" : ""}`;
	}

	async function subscribeHere() {
		const reg = await navigator.serviceWorker.register(SW_URL, { scope: "/" });
		await navigator.serviceWorker.ready;
		let sub = await reg.pushManager.getSubscription();
		if (!sub) {
			const key = await frappe.xcall(`${PUSH}.get_public_key`);
			sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyToBytes(key) });
		}
		await frappe.xcall(`${PUSH}.subscribe`, { subscription: JSON.stringify(sub.toJSON()), device: deviceLabel() });
		lsSet(LS_ENABLED, "1");
		return sub;
	}

	async function refreshPushSubscription() {
		// Re-register silently on every load so a renewed/expired subscription reaches the server.
		if (!pushSupported() || Notification.permission !== "granted" || lsGet(LS_ENABLED) !== "1") return;
		try { await subscribeHere(); } catch (e) { console.warn("Butler push refresh failed", e); }
	}

	async function enablePush(dialog) {
		try {
			const permission = await Notification.requestPermission();
			if (permission !== "granted") {
				frappe.msgprint("تم رفض الإشعارات. فعّلها من إعدادات المتصفح أو الجوال لهذا الموقع ثم حاول مرة أخرى.");
				return;
			}
			await subscribeHere();
			dialog && dialog.hide();
			const r = await frappe.xcall(`${PUSH}.send_test`);
			frappe.show_alert({ indicator: "green", message: `تم التفعيل ✅ — أرسلنا إشعاراً تجريبياً (${r.sent}/${r.devices})` }, 7);
		} catch (e) {
			console.error(e);
			frappe.msgprint(`تعذّر تفعيل الإشعارات على هذا الجهاز: ${frappe.utils.escape_html(e.message || String(e))}`);
		}
	}

	function showPushDialog() {
		const iosNeedsInstall = isIOS() && !isStandalone();
		const d = new frappe.ui.Dialog({
			title: "إشعارات واتساب على هذا الجهاز",
			fields: [{ fieldname: "info", fieldtype: "HTML" }],
			primary_action_label: iosNeedsInstall ? "فهمت" : "تفعيل الإشعارات",
			primary_action: () => (iosNeedsInstall ? d.hide() : enablePush(d)),
			secondary_action_label: "ليس الآن",
			secondary_action: () => {
				lsSet(LS_SNOOZE, String(Date.now() + 7 * 24 * 3600 * 1000));
				d.hide();
			},
		});
		const html = iosNeedsInstall
			? `<div dir="rtl" style="line-height:1.9">
				<p>على الآيفون تعمل الإشعارات فقط عند فتح النظام كتطبيق من الشاشة الرئيسية:</p>
				<ol>
					<li>افتح هذه الصفحة في <b>Safari</b>.</li>
					<li>اضغط زر <b>المشاركة</b> ثم <b>«إضافة إلى الشاشة الرئيسية»</b>.</li>
					<li>افتح النظام من الأيقونة الجديدة، ثم اضغط الزر الأخضر مرة أخرى واختر «تفعيل الإشعارات».</li>
				</ol>
				<p class="text-muted small">يتطلب iOS 16.4 أو أحدث.</p></div>`
			: `<div dir="rtl" style="line-height:1.9">
				<p>ستصلك رسائل العملاء الواردة على واتساب الشركة كإشعار على هذا الجهاز، حتى والصفحة مغلقة.</p>
				<p class="text-muted small">سيطلب المتصفح الإذن — اختر <b>سماح / Allow</b>.</p></div>`;
		d.fields_dict.info.$wrapper.html(html);
		d.show();
	}

	// expose for support / manual use: butler_wa.enablePush()
	window.butler_wa = {
		enablePush: () => showPushDialog(),
		test: () => frappe.xcall(`${PUSH}.send_test`),
		refreshBadge,
		openInbox,
		toggleLauncher,
		resetLauncher,
	};

	// ------------------------------------------------------- send document
	const DOCTYPES = ["Quotation", "Butler Quotation", "Sales Order", "Sales Invoice"];
	const METHOD = "butler_whatsapp.api";

	// target = {doctype, name, reload?}
	function openDialog(target, ctx) {
		const hasTemplates = ctx.templates.length > 0;
		const d = new frappe.ui.Dialog({
			title: __("Send via WhatsApp"),
			fields: [
				{ fieldname: "mobile", fieldtype: "Data", label: __("Mobile"), reqd: 1, default: ctx.mobile,
					description: __("Saudi numbers like 05xxxxxxxx are converted automatically") },
				{ fieldname: "window", fieldtype: "HTML" },
				{ fieldname: "print_format", fieldtype: "Select", label: __("Print Format"),
					options: ctx.print_formats.length ? ctx.print_formats : ["Standard"],
					default: ctx.default_print_format || ctx.print_formats[0] || "Standard" },
				{ fieldname: "caption", fieldtype: "Small Text", label: __("Message"), default: ctx.caption,
					depends_on: "eval:!doc.template" },
				{ fieldname: "template", fieldtype: "Select", label: __("Template (needed after 24h)"),
					options: [""].concat(ctx.templates.map((t) => t.name)), hidden: !hasTemplates,
					default: !ctx.in_window && ctx.default_template ? ctx.default_template : "" },
				{ fieldname: "params_html", fieldtype: "HTML" },
			],
			primary_action_label: __("Send"),
			primary_action(values) {
				const tpl = ctx.templates.find((t) => t.name === values.template);
				const params = [];
				if (tpl) {
					for (let i = 1; i <= tpl.variables; i++) {
						params.push(d.$wrapper.find(`[data-wa-param="${i}"]`).val() || "");
					}
				}
				frappe.call({
					method: `${METHOD}.send_document`,
					args: {
						doctype: target.doctype, name: target.name, mobile: values.mobile,
						print_format: values.print_format === "Standard" ? null : values.print_format,
						caption: values.caption, template: values.template || null,
						body_params: JSON.stringify(params),
					},
					freeze: true,
					freeze_message: __("Sending PDF over WhatsApp..."),
				}).then((r) => {
					if (r.message && r.message.ok) {
						d.hide();
						frappe.show_alert({ indicator: "green", message: __("Sent to +{0}", [r.message.to]) });
						target.reload && target.reload();
					}
				});
			},
		});

		const consent = ctx.consent || {};
		const consentNote = consent.state === "أوقف الرسائل"
			? `<div class="alert alert-danger small mb-2">⛔ هذا العميل طلب إيقاف رسائل باتلر — لا يمكن إرسال قالب له.</div>`
			: (!ctx.in_window && consent.state !== "موافق")
				? `<div class="alert alert-warning small mb-2">لا توجد موافقة مسجلة من العميل على رسائل واتساب. سجّلها من صندوق واتساب (رأس المحادثة ← الموافقة) قبل إرسال قالب.</div>`
				: "";
		const windowNote = consentNote + (ctx.in_window
			? `<div class="alert alert-success small mb-2">${__("Customer wrote within 24h — you can send the PDF directly.")}</div>`
			: `<div class="alert alert-warning small mb-2">${
				hasTemplates
					? __("No message from this number in the last 24h — choose an approved template.")
					: __("No message from this number in the last 24h and no approved document template yet. Ask the customer to send any message first.")
			}</div>`);
		d.fields_dict.window.$wrapper.html(windowNote);

		const defaults = ctx.param_defaults && ctx.param_defaults.length ? ctx.param_defaults
			: [ctx.customer_name, target.name, ctx.grand_total ? format_currency(ctx.grand_total, ctx.currency) : ""];
		const renderParams = () => {
			const tpl = ctx.templates.find((t) => t.name === d.get_value("template"));
			const html = tpl ? Array.from({ length: tpl.variables }, (_, i) =>
				`<div class="form-group"><label class="control-label">{{${i + 1}}}</label>
				 <input class="form-control" data-wa-param="${i + 1}" value="${frappe.utils.escape_html(String(defaults[i] || ""))}"></div>`).join("") : "";
			const preview = tpl && tpl.text ? `<div class="text-muted small" style="white-space:pre-wrap;border:1px dashed var(--border-color);padding:6px;border-radius:6px">${frappe.utils.escape_html(tpl.text)}</div>` : "";
			d.fields_dict.params_html.$wrapper.html(preview + html);
		};
		d.fields_dict.template.$input && d.fields_dict.template.$input.on("change", renderParams);
		d.show();
		renderParams();
	}

	function sendDocument(doctype, name, mobile, reload) {
		return frappe.call({
			method: `${METHOD}.get_send_context`,
			args: { doctype, name, mobile: mobile || null },
			freeze: true,
		}).then((r) => r.message && openDialog({ doctype, name, reload }, r.message));
	}

	function addButton(frm) {
		if (frm.is_new() || frm.doc.docstatus === 2) return;
		if (!frappe.model.can_create("WhatsApp Message")) return;
		frm.add_custom_button(__("Send via WhatsApp"), () =>
			sendDocument(frm.doctype, frm.doc.name, null, () => frm.reload_doc()));
	}

	DOCTYPES.forEach((dt) => frappe.ui.form.on(dt, { refresh: addButton }));

	// ------------------------------------------- open chat from Customer / Lead / Contact
	const PARTY_MOBILE = {
		Customer: (d) => d.mobile_no,
		Lead: (d) => d.whatsapp_no || d.mobile_no || d.phone,
		Contact: (d) => d.mobile_no || d.phone,
	};

	function addChatButton(frm) {
		if (frm.is_new() || !canUse()) return;
		const mobile = PARTY_MOBILE[frm.doctype](frm.doc);
		if (!mobile) return;
		frm.add_custom_button("💬 واتساب", () => {
			const link = frm.doctype === "Contact" ? {} : { doctype: frm.doctype, name: frm.doc.name };
			openInbox(mobile, link);
		});
	}

	Object.keys(PARTY_MOBILE).forEach((dt) => frappe.ui.form.on(dt, { refresh: addChatButton }));
	Object.assign(window.butler_wa, { sendDocument });
})();
