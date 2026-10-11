// Butler - WhatsApp inbox (Arabic, RTL, works on phones).
// Server: butler_whatsapp/inbox.py (+ access.py for who sees what)

frappe.pages["whatsapp-inbox"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: "واتساب", single_column: true });
	wrapper.butler_inbox = new ButlerInbox(page, wrapper);
};

frappe.pages["whatsapp-inbox"].on_page_show = function (wrapper) {
	wrapper.butler_inbox && wrapper.butler_inbox.on_show();
};

const BWA = "butler_whatsapp.inbox";
const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
const on_inbox_route = () => (frappe.get_route_str() || "").endsWith("whatsapp-inbox");

const VIEWS = [
	["all", "الكل"],
	["unread", "غير مقروءة"],
	["mine", "محادثاتي"],
	["unassigned", "غير مسندة"],
	["closed", "مغلقة"],
];

const EMOJIS = ("😀 😁 😂 🤣 😊 😍 😘 😎 🤔 😅 😢 😭 😡 🙏 👍 👎 👌 👏 🙌 💪 🤝 ✌️ 👋 ❤️ 💚 🔥 ⭐ ✅ ❌ ⚠️ " +
	"🎉 🎁 📌 📎 📄 📞 📍 🏠 💡 🔌 🔑 🕐 📅 💰 🚚 🛠️ ⚙️ 📷 🌙 ☀️").split(" ");

class ButlerInbox {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.number = null;
		this.info = null;
		this.messages = [];
		this.notes = [];
		this.has_more = false;
		this.conversations = [];
		this.search = "";
		this.view = "all";
		this.list_offset = 0;
		this.list_done = false;
		this.reply_to = null;
		this.ctx = { supervisor: false, agents: [], quick_replies: [] };
		this.make();
		this.bind_realtime();
		this.load_context().then(() => this.load_list());
		setInterval(() => this.is_visible() && this.load_list(true), 60000);
	}

	async load_context() {
		try {
			this.ctx = await frappe.xcall(`${BWA}.get_inbox_context`);
		} catch (e) {
			console.error(e);
		}
		this.render_views();
		if (this.ctx.can_settings && !this._settings_added) {
			this._settings_added = true;
			this.page.add_menu_item("إعدادات صندوق واتساب", () => frappe.set_route("Form", "WhatsApp Inbox Settings"));
		}
	}

	// ------------------------------------------------------------ layout
	make() {
		this.page.set_primary_action("محادثة جديدة", () => this.new_conversation_dialog(), "add");
		this.page.set_secondary_action("تحديث", () => this.refresh_all(), "refresh");
		this.page.add_menu_item("تفعيل الإشعارات على هذا الجهاز", () => window.butler_wa && window.butler_wa.enablePush());
		this.page.add_menu_item("إظهار / إخفاء زر واتساب العائم", () => window.butler_wa && window.butler_wa.toggleLauncher());
		this.page.add_menu_item("إعادة الزر العائم لمكانه", () => window.butler_wa && window.butler_wa.resetLauncher());
		this.page.add_menu_item("الردود الجاهزة", () => frappe.set_route("List", "WhatsApp Quick Reply"));
		this.page.add_menu_item("القوالب", () => frappe.set_route("List", "WhatsApp Templates"));
		this.page.add_menu_item("كل رسائل واتساب", () => frappe.set_route("List", "WhatsApp Message"));

		this.$root = $(`
			<div class="bwa">
				<div class="bwa-list">
					<div class="bwa-search">
						<input type="search" class="form-control" placeholder="بحث بالاسم أو الرقم أو العميل...">
						<div class="bwa-views"></div>
					</div>
					<div class="bwa-items"></div>
				</div>
				<div class="bwa-chat">
					<div class="bwa-placeholder">
						<div style="font-size:48px">💬</div>
						<div>اختر محادثة من القائمة</div>
						<button class="btn btn-sm btn-primary bwa-new">＋ محادثة جديدة</button>
					</div>
				</div>
			</div>`).appendTo(this.page.main);

		this.$items = this.$root.find(".bwa-items");
		this.$chat = this.$root.find(".bwa-chat");
		this.$views = this.$root.find(".bwa-views");
		this.$root.find(".bwa-new").on("click", () => this.new_conversation_dialog());

		this.$root.find(".bwa-search input").on("input", frappe.utils.debounce((e) => {
			this.search = e.target.value.trim();
			this.load_list();
		}, 350));
		this.$views.on("click", "button", (e) => {
			this.view = $(e.currentTarget).attr("data-view");
			this.render_views();
			this.load_list();
		});

		this.$items.on("scroll", () => {
			const el = this.$items.get(0);
			if (!this.list_done && !this.list_loading && el.scrollTop + el.clientHeight > el.scrollHeight - 80) {
				this.load_list(false, true);
			}
		});
		this.$items.on("click", ".bwa-item", (e) => this.open($(e.currentTarget).attr("data-number")));
	}

	render_views() {
		const views = VIEWS.filter(([v]) => this.ctx.supervisor || !["unassigned"].includes(v));
		this.$views.html(views.map(([v, label]) =>
			`<button class="${v === this.view ? "active" : ""}" data-view="${v}">${label}</button>`).join(""));
	}

	is_visible() {
		return on_inbox_route() && !document.hidden;
	}

	async on_show() {
		const params = new URLSearchParams(location.search);
		const opts = frappe.route_options || {};
		const number = opts.number || params.get("number");
		frappe.route_options = null;
		if (number && (opts.doctype || params.get("doctype"))) {
			// opened from a Customer / Lead / Contact form: link + claim, then open
			try {
				await frappe.xcall(`${BWA}.start_conversation`, {
					number, doctype: opts.doctype || params.get("doctype"), name: opts.name || params.get("name"),
				});
			} catch (e) {
				return;
			}
		}
		if (number && number.replace(/[^0-9]/g, "") !== this.number) this.open(number);
		else if (this.number) this.load_messages({ mark_read: 1, keep_scroll: true });
		this.load_list(true);
	}

	refresh_all() {
		this.load_list();
		if (this.number) this.load_messages({ mark_read: 1 });
	}

	// ---------------------------------------------------- conversations
	async load_list(quiet = false, append = false) {
		if (this.list_loading) return;
		this.list_loading = true;
		const offset = append ? this.list_offset : 0;
		try {
			const rows = await frappe.xcall(`${BWA}.get_conversations`, {
				search: this.search, offset, limit: 60, view: this.view,
			});
			this.conversations = append ? this.conversations.concat(rows) : rows;
			this.list_offset = offset + rows.length;
			this.list_done = rows.length < 60;
			this.render_list();
		} catch (e) {
			if (!quiet) console.error(e);
		} finally {
			this.list_loading = false;
		}
	}

	render_list() {
		if (!this.conversations.length) {
			const msg = this.search ? "لا توجد نتائج"
				: !this.ctx.supervisor && this.ctx.restricted ? "لا توجد محادثات مسندة لك"
				: "لا توجد محادثات";
			this.$items.html(`<div class="bwa-empty">${msg}</div>`);
			return;
		}
		this.$items.html(this.conversations.map((c) => this.list_item(c)).join(""));
	}

	list_item(c) {
		const kind = c.customer ? "customer" : c.lead ? "lead" : "";
		const initial = esc((c.title || "?").replace(/^\+/, "").trim().charAt(0) || "#");
		const last = c.last || {};
		let preview = esc(last.preview || "");
		if (last.type === "Outgoing") {
			preview = `${this.ticks(last.status)} ${last.sender ? esc(last.sender) + ": " : ""}${preview}`;
		}
		const tag = c.customer ? `<span class="bwa-tag customer">عميل</span>`
			: c.lead ? `<span class="bwa-tag lead">عميل محتمل</span>`
			: c.suggestion ? `<span class="bwa-tag" title="${esc(c.suggestion.doctype)}: ${esc(c.suggestion.title)}">🔎 مسجّل</span>` : "";
		const subs = [];
		if (c.assigned_name) subs.push(`<span class="bwa-assignee">👤 ${esc(c.assigned_name)}</span>`);
		else if (this.ctx.supervisor && this.ctx.restricted) subs.push(`<span class="bwa-unassigned">غير مسندة</span>`);
		if (c.status === "Closed") subs.push(`<span class="bwa-closed">مغلقة</span>`);
		if (last.type === "Incoming" && last.read_by && !c.unread) subs.push(`<span>👁 ${esc(last.read_by)}</span>`);
		const sla = this.ctx.sla_minutes || 0;
		if (sla && last.type === "Incoming" && c.status !== "Closed") {
			const waited = (Date.now() - frappe.datetime.str_to_obj(c.last_at).getTime()) / 60000;
			if (waited >= sla) subs.push(`<span class="bwa-overdue">⏰ بانتظار الرد ${this.wait_label(waited)}</span>`);
		}
		return `
			<div class="bwa-item ${c.unread ? "unread" : ""} ${c.number === this.number ? "active" : ""}" data-number="${esc(c.number)}">
				<div class="bwa-avatar ${kind}">${initial}</div>
				<div class="bwa-item-body">
					<div class="bwa-row">
						<div class="bwa-title">${esc(c.title)}${tag}</div>
						<div class="bwa-time">${this.short_time(c.last_at)}</div>
					</div>
					<div class="bwa-row">
						<div class="bwa-preview">${preview || "&nbsp;"}</div>
						${c.unread ? `<span class="bwa-count">${c.unread}</span>` : ""}
					</div>
					${subs.length ? `<div class="bwa-sub">${subs.join(" · ")}</div>` : ""}
				</div>
			</div>`;
	}

	// ------------------------------------------------------ conversation
	async open(number) {
		number = String(number || "").replace(/[^0-9]/g, "");
		if (!number) return;
		this.number = number;
		this.messages = [];
		this.notes = [];
		this.info = null;
		this.reply_to = null;
		this.$root.addClass("chat-open");
		this.$items.find(".bwa-item").removeClass("active");
		this.$items.find(`.bwa-item[data-number="${number}"]`).addClass("active").removeClass("unread")
			.find(".bwa-count").remove();
		this.render_chat_shell();
		try {
			await this.load_messages({ mark_read: 1 });
		} catch (e) {
			this.$msgs.html(`<div class="bwa-empty">لا يمكنك فتح هذه المحادثة</div>`);
		}
		this.load_list(true);
	}

	close_chat() {
		this.$root.removeClass("chat-open");
	}

	render_chat_shell() {
		this.$chat.html(`
			<div class="bwa-head"></div>
			<div class="bwa-window" style="display:none"></div>
			<div class="bwa-msgs"><div class="text-muted text-center p-4">جارِ التحميل...</div></div>
			<div class="bwa-replybar" style="display:none"></div>
			<div class="bwa-popup" style="display:none"></div>
			<div class="bwa-compose">
				<button class="bwa-icon-btn bwa-emoji-btn" title="إيموجي">😊</button>
				<button class="bwa-icon-btn bwa-attach" title="إرسال ملف">📎</button>
				<button class="bwa-icon-btn bwa-tpl-btn" title="إرسال قالب">📋</button>
				<textarea class="form-control" rows="1" placeholder="اكتب رسالة... ( / للردود الجاهزة )"></textarea>
				<button class="bwa-icon-btn bwa-mic" title="رسالة صوتية">🎤</button>
				<button class="bwa-icon-btn bwa-send" title="إرسال">➤</button>
			</div>
			<div class="bwa-outside" style="display:none">
				<div>⏰ مرت أكثر من 24 ساعة على آخر رسالة من العميل (أو لم يراسلك بعد). واتساب يسمح فقط بالقوالب المعتمدة.</div>
				<button class="btn btn-sm btn-primary bwa-tpl-big">📋 إرسال قالب</button>
			</div>
			<div class="bwa-recording" style="display:none">
				<span class="bwa-rec-dot"></span><span class="bwa-rec-time">0:00</span>
				<button class="btn btn-xs btn-default bwa-rec-cancel">إلغاء</button>
				<button class="btn btn-xs btn-primary bwa-rec-send">إرسال ➤</button>
			</div>`);
		this.$msgs = this.$chat.find(".bwa-msgs");
		this.$input = this.$chat.find("textarea");
		this.$popup = this.$chat.find(".bwa-popup");
		this.$chat.find(".bwa-send").on("click", () => this.send_text());
		this.$chat.find(".bwa-attach").on("click", () => this.pick_file());
		this.$chat.find(".bwa-tpl-btn, .bwa-tpl-big").on("click", () => this.template_dialog());
		this.$chat.find(".bwa-emoji-btn").on("click", () => this.toggle_emoji());
		this.$chat.find(".bwa-mic").on("click", () => this.start_recording());
		this.$chat.find(".bwa-rec-cancel").on("click", () => this.stop_recording(false));
		this.$chat.find(".bwa-rec-send").on("click", () => this.stop_recording(true));
		this.$input.on("keydown", (e) => {
			if (this.$popup.is(":visible") && this.popup_kind === "quick") {
				if (e.key === "Enter") {
					e.preventDefault();
					this.$popup.find(".bwa-qr").first().trigger("click");
					return;
				}
				if (e.key === "Escape") return this.$popup.hide();
			}
			if (e.key === "Enter" && !e.shiftKey && !frappe.is_mobile()) {
				e.preventDefault();
				this.send_text();
			}
		});
		this.$input.on("input", () => {
			const el = this.$input.get(0);
			el.style.height = "auto";
			el.style.height = Math.min(el.scrollHeight, 140) + "px";
			this.quick_reply_popup();
		});
		this.$msgs.on("click", ".bwa-more button", () => this.load_older());
		this.$msgs.on("click", ".bwa-media img", (e) => window.open(e.currentTarget.src, "_blank"));
		this.$msgs.on("click", ".bwa-reply-btn", (e) => {
			const name = $(e.currentTarget).closest(".bwa-msg").attr("data-name");
			this.set_reply(this.messages.find((m) => m.name === name));
		});
		this.$msgs.on("click", ".bwa-quote", (e) => {
			const id = $(e.currentTarget).attr("data-target");
			const $t = this.$msgs.find(`.bwa-msg[data-mid="${id}"]`);
			if ($t.length) {
				$t.get(0).scrollIntoView({ block: "center", behavior: "smooth" });
				$t.addClass("bwa-flash");
				setTimeout(() => $t.removeClass("bwa-flash"), 1200);
			}
		});
		this.$chat.on("click", ".bwa-replybar .bwa-x", () => this.set_reply(null));
	}

	async load_messages({ mark_read = 0, keep_scroll = false } = {}) {
		const number = this.number;
		const r = await frappe.xcall(`${BWA}.get_messages`, { number, mark_read });
		if (number !== this.number) return;
		const el = this.$msgs.get(0);
		const at_bottom = !keep_scroll || el.scrollTop + el.clientHeight > el.scrollHeight - 60;
		this.messages = r.messages;
		this.notes = r.notes || [];
		this.has_more = r.has_more;
		if (r.info) this.set_info(r.info);
		this.render_messages();
		if (at_bottom) this.scroll_bottom();
		this.update_launcher_badge();
	}

	async load_older() {
		if (!this.messages.length) return;
		const el = this.$msgs.get(0);
		const before_height = el.scrollHeight;
		const r = await frappe.xcall(`${BWA}.get_messages`, {
			number: this.number, before: this.messages[0].creation, mark_read: 0,
		});
		this.messages = r.messages.concat(this.messages);
		this.has_more = r.has_more;
		this.render_messages();
		el.scrollTop = el.scrollHeight - before_height;
	}

	async upsert_message(name) {
		let m;
		try {
			m = await frappe.xcall(`${BWA}.get_message`, { name });
		} catch (e) {
			return;
		}
		const i = this.messages.findIndex((x) => x.name === m.name);
		if (i >= 0) this.messages[i] = m;
		else this.messages.push(m);
		const el = this.$msgs.get(0);
		const at_bottom = el.scrollTop + el.clientHeight > el.scrollHeight - 120;
		this.render_messages();
		if (at_bottom || i < 0) this.scroll_bottom();
	}

	scroll_bottom() {
		const el = this.$msgs.get(0);
		el.scrollTop = el.scrollHeight;
		// images load later and change the height
		setTimeout(() => (el.scrollTop = el.scrollHeight), 300);
	}

	// -------------------------------------------------------- header / party
	title_of(info) {
		return info.customer_name || info.lead_name ||
			(info.contact_name && info.contact_name.replace("+", "") !== info.number ? info.contact_name : null) ||
			info.profile_name || "+" + info.number;
	}

	wait_label(minutes) {
		if (minutes < 60) return `${Math.round(minutes)} د`;
		if (minutes < 1440) return `${Math.round(minutes / 60)} س`;
		return `${Math.round(minutes / 1440)} يوم`;
	}

	consent_html(c) {
		c = c || {};
		if (c.state === "موافق") return `<span class="bwa-consent ok" title="${esc(c.source || "")}">✓ موافق على الرسائل</span>`;
		if (c.state === "أوقف الرسائل") return `<span class="bwa-consent stop" title="${esc(c.at || "")}">⛔ أوقف الرسائل</span>`;
		return `<span class="bwa-consent none">لا توجد موافقة</span>`;
	}

	consent_dialog(after) {
		const sources = this.ctx.consent_sources || [];
		const c = (this.info && this.info.consent) || {};
		const states = ["موافق", "أوقف الرسائل"].concat(this.ctx.supervisor ? [""] : []);
		const d = new frappe.ui.Dialog({
			title: "موافقة العميل على رسائل واتساب",
			fields: [
				{ fieldname: "info", fieldtype: "HTML", options: `<div class="text-muted small" dir="rtl" style="line-height:1.8">
					سياسة Meta: لا تُبدأ مراسلة العميل بقالب إلا بعد موافقته الواضحة على تلقي رسائل باتلر.
					سجّل هنا كيف وافق (أو أنه طلب الإيقاف)، ويُحفظ ذلك في سجل المحادثة.</div>` },
				{ fieldname: "state", fieldtype: "Select", label: "الحالة", reqd: 1, default: c.state || "موافق",
					options: states.map((v) => ({ value: v, label: v || "— مسح الحالة —" })) },
				{ fieldname: "source", fieldtype: "Select", label: "مصدر الموافقة", options: [""].concat(sources),
					depends_on: "eval:doc.state=='موافق'", mandatory_depends_on: "eval:doc.state=='موافق'" },
				{ fieldname: "note", fieldtype: "Small Text", label: "ملاحظة (اختياري)" },
			],
			primary_action_label: "حفظ",
			primary_action: async (v) => {
				const res = await frappe.xcall("butler_whatsapp.compliance.set_consent", {
					number: this.number, state: v.state || "", source: v.source || "", note: v.note || "",
				});
				d.hide();
				if (this.info) this.info.consent = res;
				await this.reload_info();
				frappe.show_alert({ indicator: "green", message: "تم الحفظ" });
				after && after(res);
			},
		});
		d.show();
	}

	set_info(info) {
		this.info = info;
		const title = this.title_of(info);
		const chips = [];
		if (info.customer) {
			chips.push(`<span class="bwa-chip customer">👤 عميل:
				<a href="/desk/customer/${encodeURIComponent(info.customer)}">${esc(info.customer_name || info.customer)}</a>
				<span class="bwa-x" data-unlink="Customer" title="فك الربط">✕</span></span>`);
		}
		if (info.lead) {
			chips.push(`<span class="bwa-chip lead">⭐ عميل محتمل:
				<a href="/desk/lead/${encodeURIComponent(info.lead)}">${esc(info.lead_name || info.lead)}</a>
				${info.lead_status ? `<small>(${esc(__(info.lead_status))})</small>` : ""}
				<span class="bwa-x" data-unlink="Lead" title="فك الربط">✕</span></span>`);
		}
		const linked = new Set([`Customer:${info.customer}`, `Lead:${info.lead}`]);
		const others = (info.matches || []).filter((m) => !linked.has(`${m.doctype}:${m.name}`));
		const label = { Customer: "عميل", Lead: "عميل محتمل", Contact: "جهة اتصال" };
		const match_html = others.length ? `<div class="bwa-party">
				<span class="bwa-note">🔎 هذا الرقم مسجّل أيضاً:</span>
				${others.map((m) => `<span class="bwa-chip">${label[m.doctype]}:
					<a href="/desk/${frappe.router.slug(m.doctype)}/${encodeURIComponent(m.name)}">${esc(m.title)}</a>
					${m.doctype !== "Contact" ? `<a href="#" class="bwa-link-match" data-doctype="${m.doctype}" data-name="${esc(m.name)}">ربط</a>` : ""}
				</span>`).join("")}
			</div>` : "";

		const buttons = [];
		if (!info.customer && !info.lead && info.can_create_lead) {
			buttons.push(`<button class="btn btn-xs btn-default" data-act="lead">تحويل إلى عميل محتمل</button>`);
		}
		if (!info.customer && info.can_create_customer) {
			buttons.push(`<button class="btn btn-xs btn-default" data-act="customer">${info.lead ? "تحويل إلى عميل" : "إنشاء عميل"}</button>`);
		}
		buttons.push(`<button class="btn btn-xs btn-default" data-act="link">ربط بعميل موجود</button>`);
		if (info.customer || info.lead) {
			buttons.push(`<button class="btn btn-xs btn-default" data-act="docs">📄 إرسال مستند</button>`);
		}

		// assignment + status
		let assign_html;
		if (info.supervisor && this.ctx.agents.length) {
			const opts = [`<option value="">— غير مسندة —</option>`].concat(this.ctx.agents.map((a) =>
				`<option value="${esc(a.user)}" ${a.user === info.assigned_to ? "selected" : ""}>${esc(a.name)}${a.supervisor ? " (مشرف)" : ""}</option>`));
			assign_html = `<label class="bwa-assign">مسندة إلى: <select class="form-control input-xs">${opts.join("")}</select></label>`;
		} else {
			assign_html = info.assigned_name ? `<span class="bwa-note">👤 مسندة إلى: ${esc(info.assigned_name)}</span>` : "";
		}
		const closed = info.status === "Closed";

		this.$chat.find(".bwa-head").html(`
			<div class="bwa-head-top">
				<button class="btn btn-xs btn-default bwa-back">→ رجوع</button>
				<div style="flex:1;min-width:0">
					<div class="bwa-head-name">${esc(title)} ${closed ? `<span class="bwa-closed">مغلقة</span>` : ""}</div>
					<div class="bwa-head-num">+${esc(info.number)}${info.profile_name && info.profile_name !== title ? ` · ${esc(info.profile_name)}` : ""}</div>
				</div>
				<div class="bwa-head-actions">
					${assign_html}
					<button class="btn btn-xs btn-default bwa-consent-btn" data-act="consent">${this.consent_html(info.consent)}</button>
					<button class="btn btn-xs btn-default" data-act="note">📝 ملاحظة</button>
					<button class="btn btn-xs ${closed ? "btn-primary" : "btn-default"}" data-act="status">${closed ? "إعادة فتح" : "✔ إغلاق"}</button>
				</div>
			</div>
			<div class="bwa-party">
				${chips.join("")}
				${buttons.join("")}
				${info.auto_linked ? `<span class="bwa-note">✔ تم التعرّف عليه تلقائياً من رقم الجوال</span>` : ""}
			</div>
			${match_html}`);

		const $head = this.$chat.find(".bwa-head");
		$head.find(".bwa-back").on("click", () => this.close_chat());
		$head.find("[data-act=lead]").on("click", () => this.make_lead());
		$head.find("[data-act=customer]").on("click", () => this.make_customer());
		$head.find("[data-act=link]").on("click", () => this.link_dialog());
		$head.find("[data-act=docs]").on("click", () => this.documents_dialog());
		$head.find("[data-act=note]").on("click", () => this.note_dialog());
		$head.find("[data-act=consent]").on("click", () => this.consent_dialog());
		$head.find("[data-act=status]").on("click", () => this.toggle_status());
		$head.find(".bwa-assign select").on("change", (e) => this.assign(e.target.value));
		$head.find("[data-unlink]").on("click", (e) => this.unlink($(e.currentTarget).attr("data-unlink")));
		$head.find(".bwa-link-match").on("click", (e) => {
			e.preventDefault();
			const $a = $(e.currentTarget);
			this.link($a.attr("data-doctype"), $a.attr("data-name"));
		});

		const $w = this.$chat.find(".bwa-window");
		if (!info.can_send) {
			$w.html("ليست لديك صلاحية الإرسال.").show();
		} else {
			$w.hide();
		}
		const free = info.can_send && info.in_window;
		this.$chat.find(".bwa-compose").toggle(!!free);
		this.$chat.find(".bwa-outside").toggle(!!info.can_send && !info.in_window);
	}

	default_name() {
		const i = this.info || {};
		return i.profile_name || (i.contact_name && i.contact_name.replace("+", "") !== i.number ? i.contact_name : "") || "";
	}

	async assign(user) {
		const info = await frappe.xcall(`${BWA}.assign`, { number: this.number, user });
		this.set_info(info);
		frappe.show_alert({ indicator: "green", message: user ? "تم إسناد المحادثة" : "أُلغي الإسناد" });
		this.load_messages({ keep_scroll: true });
		this.load_list(true);
	}

	async toggle_status() {
		const status = this.info.status === "Closed" ? "Open" : "Closed";
		const info = await frappe.xcall(`${BWA}.set_status`, { number: this.number, status });
		this.set_info(info);
		frappe.show_alert({ indicator: "green", message: status === "Closed" ? "أُغلقت المحادثة" : "أُعيد فتح المحادثة" });
		this.load_messages({ keep_scroll: true });
		this.load_list(true);
	}

	note_dialog() {
		const d = new frappe.ui.Dialog({
			title: "ملاحظة داخلية (لا تُرسل للعميل)",
			fields: [{ fieldname: "note", fieldtype: "Small Text", label: "الملاحظة", reqd: 1 }],
			primary_action_label: "حفظ",
			primary_action: async (v) => {
				const r = await frappe.xcall(`${BWA}.add_note`, { number: this.number, note: v.note });
				this.notes = r.notes;
				d.hide();
				this.render_messages();
				this.scroll_bottom();
			},
		});
		d.show();
	}

	make_lead() {
		const d = new frappe.ui.Dialog({
			title: "تحويل المحادثة إلى عميل محتمل",
			fields: [
				{ fieldname: "lead_name", fieldtype: "Data", label: "الاسم", reqd: 1, default: this.default_name() },
				{ fieldname: "company_name", fieldtype: "Data", label: "اسم الشركة (اختياري)" },
				{ fieldname: "mobile", fieldtype: "Data", label: "الجوال", read_only: 1, default: "+" + this.number },
				{ fieldname: "notes", fieldtype: "Small Text", label: "ملاحظات (اختياري)" },
			],
			primary_action_label: "إنشاء",
			primary_action: async (v) => {
				const r = await frappe.xcall(`${BWA}.create_lead`, { number: this.number, ...v });
				d.hide();
				frappe.show_alert({ indicator: "green", message: `تم إنشاء العميل المحتمل ${esc(r.name)}` });
				this.reload_info();
			},
		});
		d.show();
	}

	make_customer() {
		const i = this.info || {};
		const d = new frappe.ui.Dialog({
			title: i.lead ? "تحويل العميل المحتمل إلى عميل" : "إنشاء عميل من المحادثة",
			fields: [
				{ fieldname: "customer_name", fieldtype: "Data", label: "اسم العميل", reqd: 1,
					default: i.lead_name || this.default_name() },
				{ fieldname: "customer_type", fieldtype: "Select", label: "النوع",
					options: [{ value: "Individual", label: "فرد" }, { value: "Company", label: "شركة" }],
					default: "Individual" },
				{ fieldname: "mobile", fieldtype: "Data", label: "الجوال", read_only: 1, default: "+" + this.number },
			],
			primary_action_label: "إنشاء",
			primary_action: async (v) => {
				const r = await frappe.xcall(`${BWA}.create_customer`, {
					number: this.number, customer_name: v.customer_name, customer_type: v.customer_type,
				});
				d.hide();
				frappe.show_alert({ indicator: "green", message: `تم إنشاء العميل ${esc(r.name)}` });
				this.reload_info();
			},
		});
		d.show();
	}

	link_dialog() {
		const d = new frappe.ui.Dialog({
			title: "ربط المحادثة بسجل موجود",
			fields: [
				{ fieldname: "doctype", fieldtype: "Select", label: "النوع", reqd: 1,
					options: [{ value: "Customer", label: "عميل" }, { value: "Lead", label: "عميل محتمل" }],
					default: "Customer",
					onchange: () => {
						const dt = d.get_value("doctype");
						d.set_df_property("name", "options", dt);
						d.set_value("name", "");
					} },
				{ fieldname: "name", fieldtype: "Link", label: "السجل", options: "Customer", reqd: 1 },
			],
			primary_action_label: "ربط",
			primary_action: async (v) => {
				await this.link(v.doctype, v.name);
				d.hide();
			},
		});
		d.show();
	}

	async link(doctype, name) {
		const info = await frappe.xcall(`${BWA}.link_party`, { number: this.number, doctype, name });
		this.set_info(info);
		frappe.show_alert({ indicator: "green", message: "تم الربط" });
		this.load_list(true);
	}

	unlink(doctype) {
		frappe.confirm(`فك ربط هذه المحادثة عن ${doctype === "Customer" ? "العميل" : "العميل المحتمل"}؟ (لن يُحذف أي سجل)`,
			async () => {
				const info = await frappe.xcall(`${BWA}.unlink_party`, { number: this.number, doctype });
				this.set_info(info);
				this.load_list(true);
			});
	}

	async reload_info() {
		const info = await frappe.xcall(`${BWA}.get_conversation_info`, { number: this.number });
		this.set_info(info);
		this.load_list(true);
	}

	// ------------------------------------------------- new conversation
	new_conversation_dialog() {
		const d = new frappe.ui.Dialog({
			title: "محادثة جديدة",
			fields: [
				{ fieldname: "q", fieldtype: "Data", label: "ابحث عن عميل / عميل محتمل / جهة اتصال",
					description: "بالاسم أو رقم الجوال" },
				{ fieldname: "results", fieldtype: "HTML" },
				{ fieldname: "sep", fieldtype: "Section Break", label: "أو اكتب رقماً" },
				{ fieldname: "number", fieldtype: "Data", label: "رقم الجوال", description: "مثال: 05xxxxxxxx أو +9665xxxxxxxx" },
			],
			primary_action_label: "فتح المحادثة",
			primary_action: (v) => v.number && start(v.number),
		});
		const start = async (number, doctype, name) => {
			const r = await frappe.xcall(`${BWA}.start_conversation`, { number, doctype, name });
			d.hide();
			await this.open(r.number);
			this.load_list(true);
		};
		const $res = d.fields_dict.results.$wrapper;
		const label = { Customer: "عميل", Lead: "عميل محتمل", Contact: "جهة اتصال" };
		d.fields_dict.q.$input.on("input", frappe.utils.debounce(async () => {
			const q = d.get_value("q");
			if (!q || q.length < 2) return $res.empty();
			const rows = await frappe.xcall(`${BWA}.search_targets`, { q });
			$res.html(rows.length ? `<div class="bwa-results">${rows.map((r, i) => `
				<div class="bwa-result" data-i="${i}"><b>${esc(r.title)}</b>
				<span class="bwa-tag">${label[r.doctype]}</span><div class="text-muted small" dir="ltr">${esc(r.phone)}</div></div>`).join("")}</div>`
				: `<div class="text-muted small">لا نتائج بجوال مسجّل</div>`);
			$res.find(".bwa-result").on("click", (e) => {
				const r = rows[$(e.currentTarget).attr("data-i")];
				start(r.number, r.doctype, r.name);
			});
		}, 300));
		d.show();
	}

	// ------------------------------------------------------------ templates
	async template_dialog() {
		const { templates, defaults, consent, in_window, require_opt_in } = await frappe.xcall(`${BWA}.get_templates`, { number: this.number });
		if (!templates.length) {
			frappe.msgprint("لا توجد قوالب معتمدة ومفعّلة. أنشئ قالباً من «مسودات القوالب» وانتظر موافقة Meta، أو فعّل قالباً موقوفاً.");
			return;
		}
		if (consent && consent.state === "أوقف الرسائل") {
			frappe.msgprint({ title: "العميل أوقف الرسائل", indicator: "red",
				message: "طلب هذا العميل إيقاف رسائل باتلر، فلا يُرسل له أي قالب. إذا طلب العودة سجّل موافقة جديدة." });
			return;
		}
		if (require_opt_in && !in_window && (!consent || consent.state !== "موافق")) {
			frappe.confirm(
				"لا توجد موافقة مسجلة من هذا العميل على تلقي رسائل باتلر، وسياسة Meta تمنع بدء المراسلة بدونها.<br><br>هل وافق العميل؟ سجّل الموافقة ومصدرها أولاً.",
				() => this.consent_dialog((res) => res.state === "موافق" && this.template_dialog()),
			);
			return;
		}
		let header_file = null;
		const d = new frappe.ui.Dialog({
			title: "إرسال قالب معتمد",
			size: "large",
			fields: [
				{ fieldname: "template", fieldtype: "Select", label: "القالب", reqd: 1,
					options: templates.map((t) => ({ value: t.name, label: `${t.actual_name || t.name} (${t.language_code})` })) },
				{ fieldname: "params", fieldtype: "HTML" },
				{ fieldname: "preview", fieldtype: "HTML" },
			],
			primary_action_label: "إرسال",
			primary_action: async () => {
				const t = current();
				const body = d.$wrapper.find("[data-body]").map((_, el) => el.value).get();
				const head = d.$wrapper.find("[data-head]").map((_, el) => el.value).get();
				const btns = d.$wrapper.find("[data-btn]").map((_, el) => el.value).get();
				if (body.some((v) => !v.trim()) || head.some((v) => !v.trim()) || btns.some((v) => !v.trim())) {
					frappe.msgprint("أكمل كل المتغيرات");
					return;
				}
				const m = await frappe.xcall(`${BWA}.send_template`, {
					number: this.number, template: t.name, body_params: JSON.stringify(body),
					header_params: JSON.stringify(head), header_file_url: header_file,
					button_params: JSON.stringify(btns),
				}, { freeze: true });
				d.hide();
				this.push_local(m);
				this.reload_info();
				frappe.show_alert({ indicator: "green", message: "تم إرسال القالب ✅" });
			},
		});
		const current = () => templates.find((t) => t.name === d.get_value("template"));
		const render = () => {
			const t = current();
			if (!t) return;
			header_file = null;
			const inputs = [];
			for (let i = 1; i <= t.header_vars; i++) {
				inputs.push(`<div class="form-group"><label class="control-label">متغير العنوان {{${i}}}</label>
					<input class="form-control" data-head="${i}"></div>`);
			}
			for (let i = 1; i <= t.body_vars; i++) {
				const def = i === 1 ? (defaults.name || "") : "";
				inputs.push(`<div class="form-group"><label class="control-label">{{${i}}}</label>
					<input class="form-control" data-body="${i}" value="${esc(def)}"></div>`);
			}
			(t.url_buttons || []).forEach((b) => {
				inputs.push(`<div class="form-group"><label class="control-label">رابط الزر «${esc(b.label || "")}»</label>
					<input class="form-control" data-btn="${b.index}" dir="ltr" placeholder="${esc(b.url || "")}">
					<div class="text-muted small">القيمة التي تحل محل {{1}} في: ${esc(b.url || "")}</div></div>`);
			});
			if (["DOCUMENT", "IMAGE"].includes(t.header_type)) {
				inputs.push(`<div class="form-group"><label class="control-label">${t.header_type === "DOCUMENT" ? "ملف المستند" : "الصورة"}</label>
					<div><button class="btn btn-xs btn-default bwa-hdr-file">اختيار ملف</button>
					<span class="bwa-hdr-name text-muted small">${t.sample ? "سيُستخدم الملف الافتراضي للقالب" : "مطلوب"}</span></div></div>`);
			}
			d.fields_dict.params.$wrapper.html(inputs.join("") || `<div class="text-muted small">هذا القالب بلا متغيرات</div>`);
			d.fields_dict.params.$wrapper.find(".bwa-hdr-file").on("click", (e) => {
				e.preventDefault();
				new frappe.ui.FileUploader({
					allow_multiple: false, make_attachments_public: false,
					on_success: (f) => {
						header_file = f.file_url;
						d.fields_dict.params.$wrapper.find(".bwa-hdr-name").text(f.file_name);
					},
				});
			});
			const update = () => {
				const vals = d.$wrapper.find("[data-body]").map((_, el) => el.value || `{{${el.dataset.body}}}`).get();
				let text = t.template || "";
				vals.forEach((v, i) => (text = text.split(`{{${i + 1}}}`).join(v)));
				d.fields_dict.preview.$wrapper.html(`<div class="bwa-tpl-preview">
					${t.header && t.header_type === "TEXT" ? `<b>${esc(t.header)}</b><br>` : ""}
					${t.header_type && t.header_type !== "TEXT" ? `<div class="text-muted small">📎 ${t.header_type === "DOCUMENT" ? "مستند" : "صورة"}</div>` : ""}
					${esc(text).replace(/\n/g, "<br>")}
					${t.footer ? `<div class="text-muted small">${esc(t.footer)}</div>` : ""}</div>`);
			};
			d.$wrapper.find("[data-body]").on("input", update);
			update();
		};
		d.fields_dict.template.$input.on("change", render);
		d.show();
		render();
	}

	// ------------------------------------------------------------ documents
	async documents_dialog() {
		const docs = await frappe.xcall(`${BWA}.get_party_documents`, { number: this.number });
		if (!docs.length) {
			frappe.msgprint("لا توجد عروض أسعار أو أوامر بيع أو فواتير لهذا العميل.");
			return;
		}
		const label = { Quotation: "عرض سعر", "Sales Order": "أمر بيع", "Sales Invoice": "فاتورة" };
		const d = new frappe.ui.Dialog({ title: "إرسال مستند للعميل", fields: [{ fieldname: "list", fieldtype: "HTML" }] });
		d.fields_dict.list.$wrapper.html(`<div class="bwa-results">${docs.map((x, i) => `
			<div class="bwa-result" data-i="${i}">
				<b>${label[x.doctype] || x.doctype}: ${esc(x.name)}</b>
				<span class="text-muted small">${esc(x.date)} · ${esc(x.status || "")}</span>
				<div class="small">${format_currency(x.grand_total, x.currency)}</div>
			</div>`).join("")}</div>`);
		d.fields_dict.list.$wrapper.find(".bwa-result").on("click", (e) => {
			const x = docs[$(e.currentTarget).attr("data-i")];
			d.hide();
			window.butler_wa.sendDocument(x.doctype, x.name, "+" + this.number);
		});
		d.show();
	}

	// ---------------------------------------------------------- messages
	render_messages() {
		const items = this.messages.map((m) => ({ t: m.creation, m }));
		const first = this.messages.length ? this.messages[0].creation : null;
		for (const n of this.notes) {
			if (!this.has_more || !first || n.creation >= first) items.push({ t: n.creation, n });
		}
		items.sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0));
		const parts = [];
		if (this.has_more) parts.push(`<div class="bwa-more"><button class="btn btn-xs btn-default">تحميل الرسائل الأقدم</button></div>`);
		let day = null;
		for (const it of items) {
			const d = (it.t || "").slice(0, 10);
			if (d !== day) {
				day = d;
				parts.push(`<div class="bwa-day"><span>${this.day_label(it.t)}</span></div>`);
			}
			parts.push(it.m ? this.bubble(it.m) : this.note_html(it.n));
		}
		if (!items.length) parts.push(`<div class="bwa-empty">لا توجد رسائل بعد — ابدأ بإرسال قالب</div>`);
		this.$msgs.html(parts.join(""));
	}

	note_html(n) {
		if (n.kind === "event") {
			return `<div class="bwa-event">${esc(n.content)} — ${esc(n.by || "")} · ${this.clock(n.creation)}</div>`;
		}
		return `<div class="bwa-noteb"><div class="bwa-note-h">📝 ملاحظة داخلية — ${esc(n.by || "")} · ${this.clock(n.creation)}</div>
			<div class="bwa-text">${esc(n.content)}</div></div>`;
	}

	quote_html(m) {
		if (!m.reply_to) return "";
		const t = this.messages.find((x) => x.message_id === m.reply_to);
		const text = t ? (t.message || `[${t.content_type}]`) : "رسالة سابقة";
		const who = t ? (t.type === "Outgoing" ? (t.sender || "نحن") : "العميل") : "";
		return `<div class="bwa-quote" data-target="${esc(m.reply_to)}"><b>${esc(who)}</b>
			<div>${esc(String(text).slice(0, 120))}</div></div>`;
	}

	bubble(m) {
		const out = m.type === "Outgoing";
		const time = this.clock(m.creation);
		let meta;
		if (out) {
			meta = `${m.sender ? `<span>👤 ${esc(m.sender)}</span>` : ""}<span>${time}</span>${this.ticks(m.status)}`;
		} else {
			meta = m.read_by
				? `<span class="bwa-readby" title="${esc(this.full_time(m.read_at))}">👁 قرأها: ${esc(m.read_by)} · ${this.short_time(m.read_at)}</span><span>${time}</span>`
				: m.read_at
					? `<span>${time}</span>` // history from before the inbox existed
					: `<span class="bwa-unread-mark">● لم تُقرأ</span><span>${time}</span>`;
		}
		if (m.message_id) meta = `<span class="bwa-reply-btn" title="رد على هذه الرسالة">↩</span>` + meta;
		let body = this.quote_html(m);
		if (m.message_type === "Template") body += `<div class="bwa-tpl">📋 قالب${m.template ? ": " + esc(m.template) : ""}</div>`;
		body += this.content(m);
		if (out && m.error) {
			body += `<div class="bwa-error">⚠️ ${esc(m.error)}</div>`;
		}
		if (m.reference_doctype && m.reference_name) {
			body += `<div class="bwa-ref">🔗 <a href="/desk/${frappe.router.slug(m.reference_doctype)}/${encodeURIComponent(m.reference_name)}">${esc(__(m.reference_doctype))} ${esc(m.reference_name)}</a></div>`;
		}
		return `<div class="bwa-msg ${out ? "out" : "in"}" data-name="${esc(m.name)}" data-mid="${esc(m.message_id || "")}">
			<div class="bwa-bubble">${body}<div class="bwa-meta">${meta}</div></div></div>`;
	}

	content(m) {
		const type = m.content_type || "text";
		const url = m.attach ? encodeURI(m.attach) : null;
		const caption = m.message ? `<div class="bwa-text">${this.linkify(m.message)}</div>` : "";
		if (type === "text" || type === "button" || type === "interactive" || type === "location" || type === "contacts") {
			return `<div class="bwa-text">${this.linkify(m.message) || (type === "location" ? "📍 موقع" : type === "contacts" ? "👤 جهة اتصال" : "&nbsp;")}</div>`;
		}
		if (type === "reaction") return `<div class="bwa-text">تفاعل: ${esc(m.message)}</div>`;
		if (["image", "video", "audio", "document"].includes(type) && !url) {
			return `<div class="bwa-missing">📎 ${esc(type)} — الملف غير متوفر بعد</div>${caption}`;
		}
		if (type === "image") return `<div class="bwa-media"><img src="${url}" loading="lazy" alt=""></div>${caption}`;
		if (type === "video") return `<div class="bwa-media"><video src="${url}" controls preload="metadata"></video></div>${caption}`;
		if (type === "audio") return `<div class="bwa-media"><audio src="${url}" controls preload="metadata"></audio></div>${caption}`;
		if (url) {
			const name = m.file_name || "file";
			const ext = (name.split(".").pop() || "").toUpperCase();
			return `<a class="bwa-file" href="${url}" target="_blank" rel="noopener">
				<span class="bwa-file-icon">${ext === "PDF" ? "📕" : "📄"}</span>
				<span><div class="bwa-file-name">${esc(decodeURIComponent(name))}</div>
				<div class="bwa-file-sub">${esc(ext)} · فتح / تنزيل</div></span></a>${caption}`;
		}
		return `<div class="bwa-text">${this.linkify(m.message) || `[${esc(type)}]`}</div>`;
	}

	linkify(text) {
		// DOMParser never runs scripts or event handlers; we only keep the text
		const plain = /<[a-z][\s\S]*>/i.test(text || "")
			? new DOMParser().parseFromString(text, "text/html").body.textContent
			: text || "";
		const safe = esc(plain).trim();
		return safe.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
	}

	ticks(status) {
		const s = (status || "").toLowerCase();
		if (s === "read") return `<span class="bwa-ticks read" title="قُرئت">✓✓</span>`;
		if (s === "delivered") return `<span class="bwa-ticks" title="وصلت">✓✓</span>`;
		if (s === "failed") return `<span class="bwa-ticks failed" title="فشل الإرسال">⚠</span>`;
		return `<span class="bwa-ticks" title="أُرسلت">✓</span>`;
	}

	// ----------------------------------------------------------- time helpers
	moment(dt) {
		if (!dt) return null;
		return moment(frappe.datetime.convert_to_user_tz ? frappe.datetime.convert_to_user_tz(dt) : dt);
	}
	clock(dt) {
		const m = this.moment(dt);
		return m ? m.format("HH:mm") : "";
	}
	full_time(dt) {
		const m = this.moment(dt);
		return m ? m.format("YYYY-MM-DD HH:mm") : "";
	}
	short_time(dt) {
		const m = this.moment(dt);
		if (!m) return "";
		const now = moment();
		if (m.isSame(now, "day")) return m.format("HH:mm");
		if (m.isSame(now.clone().subtract(1, "day"), "day")) return "أمس";
		if (m.isAfter(now.clone().subtract(6, "day"))) return m.locale("ar").format("dddd");
		return m.format("YYYY/MM/DD");
	}
	day_label(dt) {
		const m = this.moment(dt);
		if (!m) return "";
		const now = moment();
		if (m.isSame(now, "day")) return "اليوم";
		if (m.isSame(now.clone().subtract(1, "day"), "day")) return "أمس";
		return m.format("YYYY/MM/DD");
	}

	// ------------------------------------------------------- reply / popups
	set_reply(m) {
		this.reply_to = m && m.message_id ? m : null;
		const $bar = this.$chat.find(".bwa-replybar");
		if (!this.reply_to) return $bar.hide().empty();
		const who = m.type === "Outgoing" ? (m.sender || "نحن") : "العميل";
		$bar.html(`<div class="bwa-quote"><b>↩ رد على ${esc(who)}</b><div>${esc(String(m.message || `[${m.content_type}]`).slice(0, 120))}</div></div>
			<span class="bwa-x" title="إلغاء">✕</span>`).show();
		this.$input.trigger("focus");
	}

	toggle_emoji() {
		if (this.$popup.is(":visible") && this.popup_kind === "emoji") return this.$popup.hide();
		this.popup_kind = "emoji";
		this.$popup.html(`<div class="bwa-emojis">${EMOJIS.map((e) => `<span>${e}</span>`).join("")}</div>`).show();
		this.$popup.find("span").on("click", (e) => {
			const el = this.$input.get(0);
			const pos = el.selectionStart || el.value.length;
			el.value = el.value.slice(0, pos) + e.currentTarget.textContent + el.value.slice(pos);
			el.focus();
		});
	}

	quick_reply_popup() {
		const val = this.$input.val() || "";
		if (!val.startsWith("/") || val.includes("\n")) {
			if (this.popup_kind === "quick") this.$popup.hide();
			return;
		}
		const q = val.slice(1).trim().toLowerCase();
		const rows = (this.ctx.quick_replies || []).filter((r) =>
			!q || (r.shortcut || "").toLowerCase().includes(q) || (r.title || "").toLowerCase().includes(q)).slice(0, 8);
		this.popup_kind = "quick";
		if (!rows.length) {
			this.$popup.html(`<div class="text-muted small p-2">لا توجد ردود جاهزة${this.ctx.quick_replies.length ? " مطابقة" : " — أضفها من القائمة ⋯ > الردود الجاهزة"}</div>`).show();
			return;
		}
		this.$popup.html(rows.map((r, i) => `<div class="bwa-qr" data-i="${i}"><b>/${esc(r.shortcut || "")}</b> ${esc(r.title)}
			<div class="text-muted small">${esc((r.message || "").slice(0, 90))}</div></div>`).join("")).show();
		this.$popup.find(".bwa-qr").on("click", (e) => {
			const r = rows[$(e.currentTarget).attr("data-i")];
			const name = this.info ? this.title_of(this.info).replace(/^\+\d+$/, "") : "";
			this.$input.val((r.message || "").split("{name}").join(name)).trigger("input");
			this.$popup.hide();
			this.$input.trigger("focus");
		});
	}

	// ------------------------------------------------------------ sending
	async send_text() {
		const text = (this.$input.val() || "").trim();
		if (!text || this.sending) return;
		this.sending = true;
		this.$chat.find(".bwa-send").prop("disabled", true);
		try {
			const m = await frappe.xcall(`${BWA}.send_text`, {
				number: this.number, message: text, reply_to: this.reply_to ? this.reply_to.message_id : null,
			});
			this.$input.val("").trigger("input");
			this.$popup.hide();
			this.set_reply(null);
			this.push_local(m);
		} finally {
			this.sending = false;
			this.$chat.find(".bwa-send").prop("disabled", false);
		}
	}

	pick_file() {
		const caption = (this.$input.val() || "").trim();
		new frappe.ui.FileUploader({
			allow_multiple: false,
			make_attachments_public: false,
			restrictions: { max_file_size: 100 * 1024 * 1024 },
			on_success: async (file) => {
				frappe.show_alert({ message: "جارِ إرسال الملف...", indicator: "blue" });
				const m = await frappe.xcall(`${BWA}.send_file`, {
					number: this.number, file_url: file.file_url, caption,
					reply_to: this.reply_to ? this.reply_to.message_id : null,
				});
				if (caption) this.$input.val("").trigger("input");
				this.set_reply(null);
				this.push_local(m);
				frappe.show_alert({ message: "تم إرسال الملف ✅", indicator: "green" });
			},
		});
	}

	// voice notes ---------------------------------------------------------
	async start_recording() {
		if (!navigator.mediaDevices || !window.MediaRecorder) {
			frappe.msgprint("المتصفح لا يدعم تسجيل الصوت.");
			return;
		}
		let stream;
		try {
			stream = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch (e) {
			frappe.msgprint("لم يُسمح باستخدام الميكروفون. فعّله من إعدادات المتصفح لهذا الموقع.");
			return;
		}
		const types = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus", "audio/mp4", "audio/webm"];
		const mime = types.find((t) => MediaRecorder.isTypeSupported(t)) || "";
		const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
		const chunks = [];
		rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
		this.recording = { rec, stream, chunks, mime: rec.mimeType || mime, started: Date.now() };
		rec.start();
		this.$chat.find(".bwa-compose").hide();
		this.$chat.find(".bwa-recording").css("display", "flex");
		this.rec_timer = setInterval(() => {
			const s = Math.floor((Date.now() - this.recording.started) / 1000);
			this.$chat.find(".bwa-rec-time").text(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
		}, 500);
	}

	stop_recording(send) {
		const r = this.recording;
		if (!r) return;
		clearInterval(this.rec_timer);
		this.recording = null;
		this.$chat.find(".bwa-recording").hide();
		this.$chat.find(".bwa-compose").show();
		r.rec.onstop = async () => {
			r.stream.getTracks().forEach((t) => t.stop());
			if (!send || !r.chunks.length) return;
			const type = r.mime || "audio/webm";
			const ext = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "m4a" : "webm";
			const blob = new Blob(r.chunks, { type });
			const fd = new FormData();
			fd.append("file", blob, `voice-${Date.now()}.${ext}`);
			fd.append("is_private", "1");
			frappe.show_alert({ message: "جارِ إرسال الرسالة الصوتية...", indicator: "blue" });
			try {
				const res = await fetch("/api/method/upload_file", {
					method: "POST", body: fd, headers: { "X-Frappe-CSRF-Token": frappe.csrf_token },
				});
				const j = await res.json();
				if (!res.ok) throw new Error(j.exception || res.statusText);
				const m = await frappe.xcall(`${BWA}.send_voice`, {
					number: this.number, file_url: j.message.file_url,
					reply_to: this.reply_to ? this.reply_to.message_id : null,
				});
				this.set_reply(null);
				this.push_local(m);
			} catch (e) {
				console.error(e);
				frappe.msgprint("تعذّر إرسال الرسالة الصوتية.");
			}
		};
		r.rec.stop();
	}

	push_local(m) {
		if (!this.messages.find((x) => x.name === m.name)) this.messages.push(m);
		this.render_messages();
		this.scroll_bottom();
		this.load_list(true);
	}

	// ------------------------------------------------------------ realtime
	bind_realtime() {
		const refresh_list = frappe.utils.debounce(() => this.load_list(true), 800);

		frappe.realtime.on("butler_wa_incoming", async (data) => {
			refresh_list();
			const num = String(data.from || "").replace(/[^0-9]/g, "");
			if (!this.number || num !== this.number) return;
			await this.upsert_message(data.name);
			if (this.is_visible()) {
				await frappe.xcall(`${BWA}.mark_read`, { number: this.number });
			}
			// a new message re-opens the 24h window (and maybe the conversation)
			this.reload_info();
		});

		frappe.realtime.on("butler_wa_update", (data) => {
			refresh_list();
			if (!this.number || data.number !== this.number) return;
			if (data.kind === "consent") return this.reload_info();
			if (["party", "assign", "status_change"].includes(data.kind)) {
				return this.load_messages({ keep_scroll: true }).catch(() => {
					this.$msgs.html(`<div class="bwa-empty">لم تعد هذه المحادثة مسندة إليك</div>`);
				});
			}
			if (data.kind === "read" || data.kind === "note") return this.load_messages({ keep_scroll: true });
			if (data.name) this.upsert_message(data.name);
			else this.load_messages({ keep_scroll: true });
		});

		frappe.realtime.on("butler_wa_assigned", () => refresh_list());

		document.addEventListener("visibilitychange", () => {
			if (!document.hidden && this.number && on_inbox_route()) {
				this.load_messages({ mark_read: 1, keep_scroll: true });
			}
		});
	}

	update_launcher_badge() {
		window.butler_wa && window.butler_wa.refreshBadge && window.butler_wa.refreshBadge();
	}
}
