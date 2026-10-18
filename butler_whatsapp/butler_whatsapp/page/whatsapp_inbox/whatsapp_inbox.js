// Butler - WhatsApp inbox (follows the user's language and direction, works on phones).
// Server: butler_whatsapp/inbox.py (+ access.py / teams.py for who sees what, staff_chat.py for the team chat)
// translations in the "WhatsApp" context (translations/ar.csv), falling back to Frappe's own
window.__w = window.__w || function (txt, replace) { return __(txt, replace, "WhatsApp"); };

frappe.pages["whatsapp-inbox"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __w("WhatsApp"), single_column: true });
	wrapper.butler_inbox = new ButlerInbox(page, wrapper);
};

frappe.pages["whatsapp-inbox"].on_page_show = function (wrapper) {
	wrapper.butler_inbox && wrapper.butler_inbox.on_show();
};

const BWA = "butler_whatsapp.inbox";
const STAFF = "butler_whatsapp.staff_chat";
const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
const on_inbox_route = () => (frappe.get_route_str() || "").endsWith("whatsapp-inbox");
const is_rtl = () => (frappe.utils.is_rtl ? frappe.utils.is_rtl() : document.dir === "rtl");

const VIEWS = [
	["all", __w("All")],
	["unread", __w("Unread")],
	["mine", __w("Mine")],
	["team", __w("My Teams")],
	["unassigned", __w("Unassigned")],
	["closed", __w("Closed")],
];

const OPTED_IN = "Opted In";
const OPTED_OUT = "Opted Out";

const EMOJIS = ("😀 😁 😂 🤣 😊 😍 😘 😎 🤔 😅 😢 😭 😡 🙏 👍 👎 👌 👏 🙌 💪 🤝 ✌️ 👋 ❤️ 💚 🔥 ⭐ ✅ ❌ ⚠️ " +
	"🎉 🎁 📌 📎 📄 📞 📍 🏠 💡 🔌 🔑 🕐 📅 💰 🚚 🛠️ ⚙️ 📷 🌙 ☀️").split(" ");

class ButlerInbox {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.mode = "customers"; // customers | staff
		this.number = null;
		this.info = null;
		this.messages = [];
		this.notes = [];
		this.has_more = false;
		this.conversations = [];
		this.search = "";
		this.view = "all";
		this.team_filter = "";
		this.list_offset = 0;
		this.list_done = false;
		this.reply_to = null;
		this.channels = [];
		this.channel = null;
		this.staff = null;
		this.ctx = { supervisor: false, agents: [], users: [], teams: [], my_teams: [], quick_replies: [] };
		this.make();
		this.bind_realtime();
		this.load_context().then(() => {
			this.load_list();
			this.load_channels(true);
		});
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
			this.page.add_menu_item(__w("WhatsApp Inbox Settings"), () => frappe.set_route("Form", "WhatsApp Inbox Settings"));
			this.page.add_menu_item(__w("Teams"), () => frappe.set_route("List", "WhatsApp Team"));
		}
	}

	user_name(user) {
		const u = (this.ctx.users || []).find((x) => x.user === user);
		return u ? u.name : user;
	}

	team_label(team) {
		const t = (this.ctx.teams || []).find((x) => x.name === team);
		return t ? t.label : __w(team || "");
	}

	// ------------------------------------------------------------ layout
	make() {
		this.page.set_primary_action(__w("New Conversation"), () => this.new_conversation_dialog(), "add");
		this.page.set_secondary_action(__w("Refresh"), () => this.refresh_all(), "refresh");
		this.page.add_menu_item(__w("Enable notifications on this device"), () => window.butler_wa && window.butler_wa.enablePush());
		this.page.add_menu_item(__w("Show / hide the floating WhatsApp button"), () => window.butler_wa && window.butler_wa.toggleLauncher());
		this.page.add_menu_item(__w("Move the floating button back"), () => window.butler_wa && window.butler_wa.resetLauncher());
		this.page.add_menu_item(__w("Quick Replies"), () => frappe.set_route("List", "WhatsApp Quick Reply"));
		this.page.add_menu_item(__w("Templates"), () => frappe.set_route("List", "WhatsApp Templates"));
		this.page.add_menu_item(__w("Template Drafts"), () => frappe.set_route("List", "WhatsApp Template Draft"));
		this.page.add_menu_item(__w("Inbox Performance"), () => frappe.set_route("query-report", "WhatsApp Inbox Performance"));
		this.page.add_menu_item(__w("All WhatsApp Messages"), () => frappe.set_route("List", "WhatsApp Message"));

		this.$root = $(`
			<div class="bwa">
				<div class="bwa-list">
					<div class="bwa-modes">
						<button class="active" data-mode="customers">💬 ${__w("Customers")}</button>
						<button data-mode="staff">👥 ${__w("Team Chat")} <span class="bwa-count bwa-staff-count" style="display:none"></span></button>
					</div>
					<div class="bwa-pane-customers">
						<div class="bwa-search">
							<input type="search" class="form-control" placeholder="${__w("Search by name, number or customer...")}">
							<div class="bwa-views"></div>
						</div>
						<div class="bwa-items"></div>
					</div>
					<div class="bwa-pane-staff" style="display:none">
						<div class="bwa-search">
							<button class="btn btn-sm btn-default bwa-new-dm">＋ ${__w("Message a colleague")}</button>
						</div>
						<div class="bwa-channels"></div>
					</div>
				</div>
				<div class="bwa-chat">
					<div class="bwa-placeholder">
						<div style="font-size:48px">💬</div>
						<div>${__w("Choose a conversation from the list")}</div>
						<button class="btn btn-sm btn-primary bwa-new">＋ ${__w("New Conversation")}</button>
					</div>
				</div>
			</div>`).appendTo(this.page.main);

		this.$items = this.$root.find(".bwa-items");
		this.$chat = this.$root.find(".bwa-chat");
		this.$views = this.$root.find(".bwa-views");
		this.$channels = this.$root.find(".bwa-channels");
		this.$root.find(".bwa-new").on("click", () => this.new_conversation_dialog());
		this.$root.find(".bwa-new-dm").on("click", () => this.new_direct_dialog());
		this.$root.find(".bwa-modes").on("click", "button", (e) => this.set_mode($(e.currentTarget).attr("data-mode")));

		this.$root.find(".bwa-pane-customers .bwa-search input").on("input", frappe.utils.debounce((e) => {
			this.search = e.target.value.trim();
			this.load_list();
		}, 350));
		this.$views.on("click", "button", (e) => {
			this.view = $(e.currentTarget).attr("data-view");
			this.render_views();
			this.load_list();
		});
		this.$views.on("change", "select", (e) => {
			this.team_filter = e.target.value;
			this.load_list();
		});

		this.$items.on("scroll", () => {
			const el = this.$items.get(0);
			if (!this.list_done && !this.list_loading && el.scrollTop + el.clientHeight > el.scrollHeight - 80) {
				this.load_list(false, true);
			}
		});
		this.$items.on("click", ".bwa-item", (e) => this.open($(e.currentTarget).attr("data-number")));
		this.$channels.on("click", ".bwa-item", (e) => this.open_channel($(e.currentTarget).attr("data-channel")));
	}

	set_mode(mode) {
		this.mode = mode;
		this.$root.find(".bwa-modes button").removeClass("active").filter(`[data-mode="${mode}"]`).addClass("active");
		this.$root.find(".bwa-pane-customers").toggle(mode === "customers");
		this.$root.find(".bwa-pane-staff").toggle(mode === "staff");
		if (mode === "staff") this.load_channels();
	}

	render_views() {
		const has_teams = (this.ctx.my_teams || []).length > 0;
		const views = VIEWS.filter(([v]) => v !== "team" || has_teams);
		let html = views.map(([v, label]) =>
			`<button class="${v === this.view ? "active" : ""}" data-view="${v}">${label}</button>`).join("");
		if (this.ctx.supervisor && (this.ctx.teams || []).length) {
			html += `<select class="form-control input-xs bwa-team-filter">
				<option value="">${__w("All teams")}</option>
				${this.ctx.teams.map((t) => `<option value="${esc(t.name)}" ${t.name === this.team_filter ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
			</select>`;
		}
		this.$views.html(html);
	}

	is_visible() {
		return on_inbox_route() && !document.hidden;
	}

	async on_show() {
		const params = new URLSearchParams(location.search);
		const opts = frappe.route_options || {};
		const number = opts.number || params.get("number");
		const staff = opts.staff || params.get("staff");
		frappe.route_options = null;
		if (staff) {
			this.set_mode("staff");
			this.open_channel(staff);
			return;
		}
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
		if (number && number.replace(/[^0-9]/g, "") !== this.number) {
			this.set_mode("customers");
			this.open(number);
		} else if (this.number && this.mode === "customers") this.load_messages({ mark_read: 1, keep_scroll: true });
		this.load_list(true);
	}

	refresh_all() {
		this.load_list();
		this.load_channels(true);
		if (this.mode === "customers" && this.number) this.load_messages({ mark_read: 1 });
		if (this.mode === "staff" && this.channel) this.open_channel(this.channel);
	}

	// ---------------------------------------------------- conversations
	async load_list(quiet = false, append = false) {
		if (this.list_loading) return;
		this.list_loading = true;
		const offset = append ? this.list_offset : 0;
		try {
			const rows = await frappe.xcall(`${BWA}.get_conversations`, {
				search: this.search, offset, limit: 60, view: this.view, team: this.team_filter || null,
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
			const msg = this.search ? __w("No results")
				: !this.ctx.supervisor && this.ctx.restricted ? __w("No conversations for you or your teams")
				: __w("No conversations");
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
		const tag = c.customer ? `<span class="bwa-tag customer">${__w("Customer")}</span>`
			: c.lead ? `<span class="bwa-tag lead">${__w("Lead")}</span>`
			: c.suggestion ? `<span class="bwa-tag" title="${esc(__w(c.suggestion.doctype))}: ${esc(c.suggestion.title)}">🔎 ${__w("Registered")}</span>` : "";
		const subs = [];
		if (c.team) subs.push(`<span class="bwa-team-chip">👥 ${esc(c.team_label || c.team)}</span>`);
		if (c.assigned_name) subs.push(`<span class="bwa-assignee">👤 ${esc(c.assigned_name)}</span>`);
		else if (!c.team && this.ctx.supervisor && this.ctx.restricted) subs.push(`<span class="bwa-unassigned">${__w("Unassigned")}</span>`);
		if (c.status === "Closed") subs.push(`<span class="bwa-closed">${__w("Closed")}</span>`);
		if (last.type === "Incoming" && last.read_by && !c.unread) subs.push(`<span>👁 ${esc(last.read_by)}</span>`);
		const sla = this.ctx.sla_minutes || 0;
		if (sla && last.type === "Incoming" && c.status !== "Closed") {
			const waited = (Date.now() - frappe.datetime.str_to_obj(c.last_at).getTime()) / 60000;
			if (waited >= sla) subs.push(`<span class="bwa-overdue">⏰ ${__w("Waiting for a reply")} ${this.wait_label(waited)}</span>`);
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
		this.channel = null;
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
			this.$msgs.html(`<div class="bwa-empty">${__w("You cannot open this conversation")}</div>`);
		}
		this.load_list(true);
	}

	close_chat() {
		this.$root.removeClass("chat-open");
	}

	back_label() {
		return is_rtl() ? `→ ${__w("Back")}` : `← ${__w("Back")}`;
	}

	render_chat_shell() {
		this.$chat.html(`
			<div class="bwa-head"></div>
			<div class="bwa-window" style="display:none"></div>
			<div class="bwa-msgs"><div class="text-muted text-center p-4">${__w("Loading...")}</div></div>
			<div class="bwa-replybar" style="display:none"></div>
			<div class="bwa-popup" style="display:none"></div>
			<div class="bwa-compose">
				<button class="bwa-icon-btn bwa-emoji-btn" title="${__w("Emoji")}">😊</button>
				<button class="bwa-icon-btn bwa-attach" title="${__w("Send a file")}">📎</button>
				<button class="bwa-icon-btn bwa-tpl-btn" title="${__w("Send a template")}">📋</button>
				<textarea class="form-control" rows="1" placeholder="${__w("Type a message... ( / for quick replies )")}"></textarea>
				<button class="bwa-icon-btn bwa-mic" title="${__w("Voice message")}">🎤</button>
				<button class="bwa-icon-btn bwa-send" title="${__w("Send")}">➤</button>
			</div>
			<div class="bwa-outside" style="display:none">
				<div>⏰ ${__w("More than 24 hours have passed since the customer's last message (or they have not written yet). WhatsApp only allows approved templates.")}</div>
				<button class="btn btn-sm btn-primary bwa-tpl-big">📋 ${__w("Send a template")}</button>
			</div>
			<div class="bwa-recording" style="display:none">
				<span class="bwa-rec-dot"></span><span class="bwa-rec-time">0:00</span>
				<button class="btn btn-xs btn-default bwa-rec-cancel">${__w("Cancel")}</button>
				<button class="btn btn-xs btn-primary bwa-rec-send">${__w("Send")} ➤</button>
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
			this.autosize(this.$input);
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

	autosize($el) {
		const el = $el.get(0);
		el.style.height = "auto";
		el.style.height = Math.min(el.scrollHeight, 140) + "px";
	}

	async load_messages({ mark_read = 0, keep_scroll = false } = {}) {
		const number = this.number;
		const r = await frappe.xcall(`${BWA}.get_messages`, { number, mark_read });
		if (number !== this.number || this.channel) return;
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
		if (minutes < 60) return __w("{0} min", [Math.round(minutes)]);
		if (minutes < 1440) return __w("{0} h", [Math.round(minutes / 60)]);
		return __w("{0} d", [Math.round(minutes / 1440)]);
	}

	consent_html(c) {
		c = c || {};
		if (c.state === OPTED_IN) return `<span class="bwa-consent ok" title="${esc(__w(c.source || ""))}">✓ ${__w("Agreed to messages")}</span>`;
		if (c.state === OPTED_OUT) return `<span class="bwa-consent stop" title="${esc(c.at || "")}">⛔ ${__w("Opted out")}</span>`;
		return `<span class="bwa-consent none">${__w("No consent")}</span>`;
	}

	consent_dialog(after) {
		const sources = this.ctx.consent_sources || [];
		const c = (this.info && this.info.consent) || {};
		const states = [OPTED_IN, OPTED_OUT].concat(this.ctx.supervisor ? [""] : []);
		const d = new frappe.ui.Dialog({
			title: __w("Customer consent to WhatsApp messages"),
			fields: [
				{ fieldname: "info", fieldtype: "HTML", options: `<div class="text-muted small" style="line-height:1.8">
					${__w("Meta policy: do not start a conversation with a template until the customer clearly agreed to receive Butler's messages. Record here how they agreed (or that they asked to stop); it is saved in the conversation history.")}</div>` },
				{ fieldname: "state", fieldtype: "Select", label: __w("Status"), reqd: 1, default: c.state || OPTED_IN,
					options: states.map((v) => ({ value: v, label: v ? __w(v) : `— ${__w("Clear the status")} —` })) },
				{ fieldname: "source", fieldtype: "Select", label: __w("Source of the consent"),
					options: [{ value: "", label: "" }].concat(sources.map((s) => ({ value: s, label: __w(s) }))),
					depends_on: `eval:doc.state=='${OPTED_IN}'`, mandatory_depends_on: `eval:doc.state=='${OPTED_IN}'` },
				{ fieldname: "note", fieldtype: "Small Text", label: __w("Note (optional)") },
			],
			primary_action_label: __w("Save"),
			primary_action: async (v) => {
				const res = await frappe.xcall("butler_whatsapp.compliance.set_consent", {
					number: this.number, state: v.state || "", source: v.source || "", note: v.note || "",
				});
				d.hide();
				if (this.info) this.info.consent = res;
				await this.reload_info();
				frappe.show_alert({ indicator: "green", message: __w("Saved") });
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
			chips.push(`<span class="bwa-chip customer">👤 ${__w("Customer")}:
				<a href="/desk/customer/${encodeURIComponent(info.customer)}">${esc(info.customer_name || info.customer)}</a>
				<span class="bwa-x" data-unlink="Customer" title="${__w("Unlink")}">✕</span></span>`);
		}
		if (info.lead) {
			chips.push(`<span class="bwa-chip lead">⭐ ${__w("Lead")}:
				<a href="/desk/lead/${encodeURIComponent(info.lead)}">${esc(info.lead_name || info.lead)}</a>
				${info.lead_status ? `<small>(${esc(__w(info.lead_status))})</small>` : ""}
				<span class="bwa-x" data-unlink="Lead" title="${__w("Unlink")}">✕</span></span>`);
		}
		const linked = new Set([`Customer:${info.customer}`, `Lead:${info.lead}`]);
		const others = (info.matches || []).filter((m) => !linked.has(`${m.doctype}:${m.name}`));
		const match_html = others.length ? `<div class="bwa-party">
				<span class="bwa-note">🔎 ${__w("This number is also registered as:")}</span>
				${others.map((m) => `<span class="bwa-chip">${__w(m.doctype)}:
					<a href="/desk/${frappe.router.slug(m.doctype)}/${encodeURIComponent(m.name)}">${esc(m.title)}</a>
					${m.doctype !== "Contact" ? `<a href="#" class="bwa-link-match" data-doctype="${m.doctype}" data-name="${esc(m.name)}">${__w("Link")}</a>` : ""}
				</span>`).join("")}
			</div>` : "";

		const buttons = [];
		if (!info.customer && !info.lead && info.can_create_lead) {
			buttons.push(`<button class="btn btn-xs btn-default" data-act="lead">${__w("Convert to Lead")}</button>`);
		}
		if (!info.customer && info.can_create_customer) {
			buttons.push(`<button class="btn btn-xs btn-default" data-act="customer">${info.lead ? __w("Convert to Customer") : __w("Create Customer")}</button>`);
		}
		buttons.push(`<button class="btn btn-xs btn-default" data-act="link">${__w("Link to an existing record")}</button>`);
		if (info.customer || info.lead) {
			buttons.push(`<button class="btn btn-xs btn-default" data-act="docs">📄 ${__w("Send a document")}</button>`);
		}

		// team + assignee + participants
		const owner = [];
		if (info.team) owner.push(`👥 ${esc(info.team_label || info.team)}`);
		if (info.assigned_name) owner.push(`👤 ${esc(info.assigned_name)}`);
		const owner_text = owner.length ? owner.join(" · ") : __w("Unassigned");
		const participants = (info.participants || []).map((p) =>
			`<span class="bwa-chip">🙋 ${esc(p.name)} <span class="bwa-x" data-unpart="${esc(p.user)}" title="${__w("Remove")}">✕</span></span>`).join("");
		const closed = info.status === "Closed";

		this.$chat.find(".bwa-head").html(`
			<div class="bwa-head-top">
				<button class="btn btn-xs btn-default bwa-back">${this.back_label()}</button>
				<div style="flex:1;min-width:0">
					<div class="bwa-head-name">${esc(title)} ${closed ? `<span class="bwa-closed">${__w("Closed")}</span>` : ""}</div>
					<div class="bwa-head-num">+${esc(info.number)}${info.profile_name && info.profile_name !== title ? ` · ${esc(info.profile_name)}` : ""}</div>
				</div>
				<div class="bwa-head-actions">
					<button class="btn btn-xs btn-default bwa-owner" data-act="assign" title="${__w("Assign or transfer")}">${owner_text} ▾</button>
					<button class="btn btn-xs btn-default bwa-consent-btn" data-act="consent">${this.consent_html(info.consent)}</button>
					<button class="btn btn-xs btn-default" data-act="note">📝 ${__w("Note")}</button>
					<button class="btn btn-xs btn-default" data-act="discuss">👥 ${__w("Discuss")}</button>
					<button class="btn btn-xs ${closed ? "btn-primary" : "btn-default"}" data-act="status">${closed ? __w("Reopen") : "✔ " + __w("Close")}</button>
				</div>
			</div>
			<div class="bwa-party">
				${chips.join("")}
				${buttons.join("")}
				${info.auto_linked ? `<span class="bwa-note">✔ ${__w("Recognised automatically from the mobile number")}</span>` : ""}
			</div>
			${participants ? `<div class="bwa-party"><span class="bwa-note">${__w("Also following:")}</span>${participants}</div>` : ""}
			${match_html}`);

		const $head = this.$chat.find(".bwa-head");
		$head.find(".bwa-back").on("click", () => this.close_chat());
		$head.find("[data-act=lead]").on("click", () => this.make_lead());
		$head.find("[data-act=customer]").on("click", () => this.make_customer());
		$head.find("[data-act=link]").on("click", () => this.link_dialog());
		$head.find("[data-act=docs]").on("click", () => this.documents_dialog());
		$head.find("[data-act=note]").on("click", () => this.note_dialog());
		$head.find("[data-act=discuss]").on("click", () => this.discuss_dialog());
		$head.find("[data-act=consent]").on("click", () => this.consent_dialog());
		$head.find("[data-act=status]").on("click", () => this.toggle_status());
		$head.find("[data-act=assign]").on("click", () => this.assign_dialog());
		$head.find("[data-unlink]").on("click", (e) => this.unlink($(e.currentTarget).attr("data-unlink")));
		$head.find("[data-unpart]").on("click", (e) => this.remove_participant($(e.currentTarget).attr("data-unpart")));
		$head.find(".bwa-link-match").on("click", (e) => {
			e.preventDefault();
			const $a = $(e.currentTarget);
			this.link($a.attr("data-doctype"), $a.attr("data-name"));
		});

		const $w = this.$chat.find(".bwa-window");
		if (!info.can_send) {
			$w.html(__w("You are not allowed to send messages.")).show();
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

	user_options(only_agents = false) {
		return [{ value: "", label: `— ${__w("Nobody")} —` }].concat((this.ctx.users || [])
			.filter((u) => !only_agents || u.agent)
			.map((u) => ({ value: u.user, label: u.name + (u.supervisor ? ` (${__w("Supervisor")})` : "") })));
	}

	assign_dialog() {
		const info = this.info || {};
		const teams = this.ctx.teams || [];
		const d = new frappe.ui.Dialog({
			title: __w("Assign or transfer the conversation"),
			fields: [
				{ fieldname: "team", fieldtype: "Select", label: __w("Team"), default: info.team || "",
					options: [{ value: "", label: `— ${__w("No team")} —` }].concat(teams.map((t) => ({ value: t.name, label: t.label }))),
					onchange: () => show_members() },
				{ fieldname: "members_html", fieldtype: "HTML" },
				{ fieldname: "user", fieldtype: "Autocomplete", label: __w("Person"), default: info.assigned_to || "",
					options: this.user_options(),
					description: __w("Any user can be chosen; the WhatsApp Agent role is added automatically if needed.") },
				{ fieldname: "note", fieldtype: "Small Text", label: __w("Note for the history (optional)") },
			],
			primary_action_label: __w("Save"),
			primary_action: async (v) => {
				const res = await frappe.xcall(`${BWA}.assign`, {
					number: this.number, user: v.user || "", team: v.team || "", note: v.note || "",
				});
				d.hide();
				frappe.show_alert({ indicator: "green", message: __w("Saved") });
				this.after_assign(res);
			},
		});
		const show_members = () => {
			const t = teams.find((x) => x.name === d.get_value("team"));
			d.fields_dict.members_html.$wrapper.html(t ? `<div class="text-muted small mb-2">${__w("Members")}: ${
				t.members.length ? t.members.map((u) => `<a href="#" class="bwa-pick" data-user="${esc(u)}">${esc(this.user_name(u))}</a>`).join(" · ")
				: __w("no members yet")}</div>` : "");
			d.fields_dict.members_html.$wrapper.find(".bwa-pick").on("click", (e) => {
				e.preventDefault();
				d.set_value("user", $(e.currentTarget).attr("data-user"));
			});
		};
		d.show();
		show_members();
	}

	after_assign(info) {
		if (!info) return;
		if (info.lost_access) {
			// handed over to someone else: this user no longer sees it
			this.$msgs && this.$msgs.html(`<div class="bwa-empty">${__w("This conversation is no longer assigned to you")}</div>`);
			this.$chat.find(".bwa-compose, .bwa-outside").hide();
			this.load_list(true);
			return;
		}
		this.set_info(info);
		this.load_messages({ keep_scroll: true }).catch(() => {
			this.$msgs.html(`<div class="bwa-empty">${__w("This conversation is no longer assigned to you")}</div>`);
		});
		this.load_list(true);
	}

	async remove_participant(user) {
		const info = await frappe.xcall(`${BWA}.remove_participant`, { number: this.number, user });
		this.after_assign(info);
	}

	async toggle_status() {
		const status = this.info.status === "Closed" ? "Open" : "Closed";
		const info = await frappe.xcall(`${BWA}.set_status`, { number: this.number, status });
		this.set_info(info);
		frappe.show_alert({ indicator: "green", message: status === "Closed" ? __w("Conversation closed") : __w("Conversation reopened") });
		this.load_messages({ keep_scroll: true });
		this.load_list(true);
	}

	note_dialog() {
		const d = new frappe.ui.Dialog({
			title: __w("Internal note (not sent to the customer)"),
			fields: [
				{ fieldname: "note", fieldtype: "Small Text", label: __w("Note"), reqd: 1 },
				{ fieldname: "mentions", fieldtype: "MultiSelectList", label: __w("Mention colleagues"),
					description: __w("They get a notification and can open this conversation."),
					get_data: (txt) => (this.ctx.users || [])
						.filter((u) => u.user !== frappe.session.user && (!txt || u.name.toLowerCase().includes(txt.toLowerCase())))
						.slice(0, 30).map((u) => ({ value: u.user, description: u.name })) },
			],
			primary_action_label: __w("Save"),
			primary_action: async (v) => {
				const r = await frappe.xcall(`${BWA}.add_note`, {
					number: this.number, note: v.note, mentions: JSON.stringify(v.mentions || []),
				});
				this.notes = r.notes;
				d.hide();
				this.render_messages();
				this.scroll_bottom();
				if ((v.mentions || []).length) this.reload_info();
			},
		});
		d.show();
	}

	discuss_dialog() {
		// share this customer conversation into a team chat or a direct chat with a colleague
		const teams = (this.ctx.teams || []).filter((t) => this.ctx.supervisor || (this.ctx.my_teams || []).includes(t.name));
		const options = teams.map((t) => ({ value: `team:${t.name}`, label: `👥 ${t.label}` }))
			.concat((this.ctx.users || []).filter((u) => u.user !== frappe.session.user)
				.map((u) => ({ value: `user:${u.user}`, label: `👤 ${u.name}` })));
		const d = new frappe.ui.Dialog({
			title: __w("Discuss with colleagues"),
			fields: [
				{ fieldname: "to", fieldtype: "Autocomplete", label: __w("Team chat or colleague"), reqd: 1, options },
				{ fieldname: "message", fieldtype: "Small Text", label: __w("Message"), reqd: 1 },
			],
			primary_action_label: __w("Send"),
			primary_action: async (v) => {
				let channel = v.to;
				if (channel.startsWith("user:")) {
					channel = (await frappe.xcall(`${STAFF}.open_direct`, { user: channel.slice(5) })).channel;
				}
				await frappe.xcall(`${STAFF}.send_staff_message`, { channel, message: v.message, ref_number: this.number });
				d.hide();
				frappe.show_alert({ indicator: "green", message: __w("Sent to the team chat") });
				this.load_channels(true);
			},
		});
		d.show();
	}

	make_lead() {
		const d = new frappe.ui.Dialog({
			title: __w("Convert the conversation to a Lead"),
			fields: [
				{ fieldname: "lead_name", fieldtype: "Data", label: __w("Name"), reqd: 1, default: this.default_name() },
				{ fieldname: "company_name", fieldtype: "Data", label: __w("Company name (optional)") },
				{ fieldname: "mobile", fieldtype: "Data", label: __w("Mobile"), read_only: 1, default: "+" + this.number },
				{ fieldname: "notes", fieldtype: "Small Text", label: __w("Notes (optional)") },
			],
			primary_action_label: __w("Create"),
			primary_action: async (v) => {
				const r = await frappe.xcall(`${BWA}.create_lead`, { number: this.number, ...v });
				d.hide();
				frappe.show_alert({ indicator: "green", message: __w("Lead {0} created", [esc(r.name)]) });
				this.reload_info();
			},
		});
		d.show();
	}

	make_customer() {
		const i = this.info || {};
		const d = new frappe.ui.Dialog({
			title: i.lead ? __w("Convert the Lead to a Customer") : __w("Create a Customer from the conversation"),
			fields: [
				{ fieldname: "customer_name", fieldtype: "Data", label: __w("Customer Name"), reqd: 1,
					default: i.lead_name || this.default_name() },
				{ fieldname: "customer_type", fieldtype: "Select", label: __w("Type"),
					options: [{ value: "Individual", label: __w("Individual") }, { value: "Company", label: __w("Company") }],
					default: "Individual" },
				{ fieldname: "mobile", fieldtype: "Data", label: __w("Mobile"), read_only: 1, default: "+" + this.number },
			],
			primary_action_label: __w("Create"),
			primary_action: async (v) => {
				const r = await frappe.xcall(`${BWA}.create_customer`, {
					number: this.number, customer_name: v.customer_name, customer_type: v.customer_type,
				});
				d.hide();
				frappe.show_alert({ indicator: "green", message: __w("Customer {0} created", [esc(r.name)]) });
				this.reload_info();
			},
		});
		d.show();
	}

	link_dialog() {
		const d = new frappe.ui.Dialog({
			title: __w("Link the conversation to an existing record"),
			fields: [
				{ fieldname: "doctype", fieldtype: "Select", label: __w("Type"), reqd: 1,
					options: [{ value: "Customer", label: __w("Customer") }, { value: "Lead", label: __w("Lead") }],
					default: "Customer",
					onchange: () => {
						const dt = d.get_value("doctype");
						d.set_df_property("name", "options", dt);
						d.set_value("name", "");
					} },
				{ fieldname: "name", fieldtype: "Link", label: __w("Record"), options: "Customer", reqd: 1 },
			],
			primary_action_label: __w("Link"),
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
		frappe.show_alert({ indicator: "green", message: __w("Linked") });
		this.load_list(true);
	}

	unlink(doctype) {
		frappe.confirm(__w("Unlink this conversation from the {0}? (No record is deleted)", [__w(doctype)]),
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
			title: __w("New Conversation"),
			fields: [
				{ fieldname: "q", fieldtype: "Data", label: __w("Search for a customer / lead / contact"),
					description: __w("By name or mobile number") },
				{ fieldname: "results", fieldtype: "HTML" },
				{ fieldname: "sep", fieldtype: "Section Break", label: __w("Or type a number") },
				{ fieldname: "number", fieldtype: "Data", label: __w("Mobile Number"), description: __w("Example: 05xxxxxxxx or +9665xxxxxxxx") },
			],
			primary_action_label: __w("Open conversation"),
			primary_action: (v) => v.number && start(v.number),
		});
		const start = async (number, doctype, name) => {
			const r = await frappe.xcall(`${BWA}.start_conversation`, { number, doctype, name });
			d.hide();
			this.set_mode("customers");
			await this.open(r.number);
			this.load_list(true);
		};
		const $res = d.fields_dict.results.$wrapper;
		d.fields_dict.q.$input.on("input", frappe.utils.debounce(async () => {
			const q = d.get_value("q");
			if (!q || q.length < 2) return $res.empty();
			const rows = await frappe.xcall(`${BWA}.search_targets`, { q });
			$res.html(rows.length ? `<div class="bwa-results">${rows.map((r, i) => `
				<div class="bwa-result" data-i="${i}"><b>${esc(r.title)}</b>
				<span class="bwa-tag">${__w(r.doctype)}</span><div class="text-muted small bwa-ltr">${esc(r.phone)}</div></div>`).join("")}</div>`
				: `<div class="text-muted small">${__w("No results with a registered mobile")}</div>`);
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
			frappe.msgprint(__w("There are no approved, switched-on templates. Create one from Template Drafts and wait for Meta's approval, or switch on a template that is off."));
			return;
		}
		if (consent && consent.state === OPTED_OUT) {
			frappe.msgprint({ title: __w("Customer opted out"), indicator: "red",
				message: __w("This customer asked to stop Butler's messages, so no template can be sent. If they ask to come back, record a new consent.") });
			return;
		}
		if (require_opt_in && !in_window && (!consent || consent.state !== OPTED_IN)) {
			frappe.confirm(
				__w("There is no recorded consent from this customer to receive Butler's messages, and Meta's policy forbids starting a conversation without it.") +
				"<br><br>" + __w("Did the customer agree? Record the consent and its source first."),
				() => this.consent_dialog((res) => res.state === OPTED_IN && this.template_dialog()),
			);
			return;
		}
		let header_file = null;
		const d = new frappe.ui.Dialog({
			title: __w("Send an approved template"),
			size: "large",
			fields: [
				{ fieldname: "template", fieldtype: "Select", label: __w("Template"), reqd: 1,
					options: templates.map((t) => ({ value: t.name, label: `${t.actual_name || t.name} (${t.language_code})` })) },
				{ fieldname: "params", fieldtype: "HTML" },
				{ fieldname: "preview", fieldtype: "HTML" },
			],
			primary_action_label: __w("Send"),
			primary_action: async () => {
				const t = current();
				const body = d.$wrapper.find("[data-body]").map((_, el) => el.value).get();
				const head = d.$wrapper.find("[data-head]").map((_, el) => el.value).get();
				const btns = d.$wrapper.find("[data-btn]").map((_, el) => el.value).get();
				if (body.some((v) => !v.trim()) || head.some((v) => !v.trim()) || btns.some((v) => !v.trim())) {
					frappe.msgprint(__w("Fill in every variable"));
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
				frappe.show_alert({ indicator: "green", message: __w("Template sent") + " ✅" });
			},
		});
		const current = () => templates.find((t) => t.name === d.get_value("template"));
		const render = () => {
			const t = current();
			if (!t) return;
			header_file = null;
			const rtl = (t.language_code || "").startsWith("ar");
			const inputs = [];
			for (let i = 1; i <= t.header_vars; i++) {
				inputs.push(`<div class="form-group"><label class="control-label">${__w("Header variable")} {{${i}}}</label>
					<input class="form-control" data-head="${i}" dir="${rtl ? "rtl" : "ltr"}"></div>`);
			}
			for (let i = 1; i <= t.body_vars; i++) {
				const def = i === 1 ? (defaults.name || "") : "";
				inputs.push(`<div class="form-group"><label class="control-label">{{${i}}}</label>
					<input class="form-control" data-body="${i}" value="${esc(def)}" dir="${rtl ? "rtl" : "ltr"}"></div>`);
			}
			(t.url_buttons || []).forEach((b) => {
				inputs.push(`<div class="form-group"><label class="control-label">${__w("Link of the button \"{0}\"", [esc(b.label || "")])}</label>
					<input class="form-control bwa-ltr" data-btn="${b.index}" dir="ltr" placeholder="${esc(b.url || "")}">
					<div class="text-muted small">${__w("The value that replaces {{1}} in:")} ${esc(b.url || "")}</div></div>`);
			});
			if (["DOCUMENT", "IMAGE"].includes(t.header_type)) {
				inputs.push(`<div class="form-group"><label class="control-label">${t.header_type === "DOCUMENT" ? __w("Document file") : __w("Image")}</label>
					<div><button class="btn btn-xs btn-default bwa-hdr-file">${__w("Choose a file")}</button>
					<span class="bwa-hdr-name text-muted small">${t.sample ? __w("The template's default file will be used") : __w("Required")}</span></div></div>`);
			}
			d.fields_dict.params.$wrapper.html(inputs.join("") || `<div class="text-muted small">${__w("This template has no variables")}</div>`);
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
				d.fields_dict.preview.$wrapper.html(`<div class="bwa-tpl-preview" dir="${rtl ? "rtl" : "ltr"}">
					${t.header && t.header_type === "TEXT" ? `<b>${esc(t.header)}</b><br>` : ""}
					${t.header_type && t.header_type !== "TEXT" ? `<div class="text-muted small">📎 ${t.header_type === "DOCUMENT" ? __w("Document") : __w("Image")}</div>` : ""}
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
			frappe.msgprint(__w("There are no quotations, sales orders or invoices for this customer."));
			return;
		}
		const d = new frappe.ui.Dialog({ title: __w("Send a document to the customer"), fields: [{ fieldname: "list", fieldtype: "HTML" }] });
		d.fields_dict.list.$wrapper.html(`<div class="bwa-results">${docs.map((x, i) => `
			<div class="bwa-result" data-i="${i}">
				<b>${__w(x.doctype)}: ${esc(x.name)}</b>
				<span class="text-muted small">${esc(x.date)} · ${esc(__w(x.status || ""))}</span>
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
		if (this.has_more) parts.push(`<div class="bwa-more"><button class="btn btn-xs btn-default">${__w("Load older messages")}</button></div>`);
		let day = null;
		for (const it of items) {
			const d = (it.t || "").slice(0, 10);
			if (d !== day) {
				day = d;
				parts.push(`<div class="bwa-day"><span>${this.day_label(it.t)}</span></div>`);
			}
			parts.push(it.m ? this.bubble(it.m) : this.note_html(it.n));
		}
		if (!items.length) parts.push(`<div class="bwa-empty">${__w("No messages yet - start by sending a template")}</div>`);
		this.$msgs.html(parts.join(""));
	}

	note_html(n) {
		if (n.kind === "event") {
			return `<div class="bwa-event">${esc(n.content)} — ${esc(n.by || "")} · ${this.clock(n.creation)}</div>`;
		}
		return `<div class="bwa-noteb"><div class="bwa-note-h">📝 ${__w("Internal note")} — ${esc(n.by || "")} · ${this.clock(n.creation)}</div>
			<div class="bwa-text">${esc(n.content)}</div></div>`;
	}

	quote_html(m) {
		if (!m.reply_to) return "";
		const t = this.messages.find((x) => x.message_id === m.reply_to);
		const text = t ? (t.message || `[${t.content_type}]`) : __w("An earlier message");
		const who = t ? (t.type === "Outgoing" ? (t.sender || __w("Us")) : __w("Customer")) : "";
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
				? `<span class="bwa-readby" title="${esc(this.full_time(m.read_at))}">👁 ${__w("Read by")}: ${esc(m.read_by)} · ${this.short_time(m.read_at)}</span><span>${time}</span>`
				: m.read_at
					? `<span>${time}</span>` // history from before the inbox existed
					: `<span class="bwa-unread-mark">● ${__w("Unread")}</span><span>${time}</span>`;
		}
		if (m.message_id) meta = `<span class="bwa-reply-btn" title="${__w("Reply to this message")}">↩</span>` + meta;
		let body = this.quote_html(m);
		if (m.message_type === "Template") body += `<div class="bwa-tpl">📋 ${__w("Template")}${m.template ? ": " + esc(m.template) : ""}</div>`;
		body += this.content(m);
		if (out && m.error) {
			body += `<div class="bwa-error">⚠️ ${esc(m.error)}</div>`;
		}
		if (m.reference_doctype && m.reference_name) {
			body += `<div class="bwa-ref">🔗 <a href="/desk/${frappe.router.slug(m.reference_doctype)}/${encodeURIComponent(m.reference_name)}">${esc(__w(m.reference_doctype))} ${esc(m.reference_name)}</a></div>`;
		}
		return `<div class="bwa-msg ${out ? "out" : "in"}" data-name="${esc(m.name)}" data-mid="${esc(m.message_id || "")}">
			<div class="bwa-bubble" dir="auto">${body}<div class="bwa-meta">${meta}</div></div></div>`;
	}

	content(m) {
		const type = m.content_type || "text";
		const url = m.attach ? encodeURI(m.attach) : null;
		const caption = m.message ? `<div class="bwa-text">${this.linkify(m.message)}</div>` : "";
		if (type === "text" || type === "button" || type === "interactive" || type === "location" || type === "contacts") {
			return `<div class="bwa-text">${this.linkify(m.message) || (type === "location" ? "📍 " + __w("Location") : type === "contacts" ? "👤 " + __w("Contact") : "&nbsp;")}</div>`;
		}
		if (type === "reaction") return `<div class="bwa-text">${__w("Reaction")}: ${esc(m.message)}</div>`;
		if (["image", "video", "audio", "document"].includes(type) && !url) {
			return `<div class="bwa-missing">📎 ${esc(__w(type))} — ${__w("the file is not available yet")}</div>${caption}`;
		}
		if (type === "image") return `<div class="bwa-media"><img src="${url}" loading="lazy" alt=""></div>${caption}`;
		if (type === "video") return `<div class="bwa-media"><video src="${url}" controls preload="metadata"></video></div>${caption}`;
		if (type === "audio") return `<div class="bwa-media"><audio src="${url}" controls preload="metadata"></audio></div>${caption}`;
		if (url) return this.file_html(url, m.file_name) + caption;
		return `<div class="bwa-text">${this.linkify(m.message) || `[${esc(type)}]`}</div>`;
	}

	file_html(url, file_name) {
		const name = file_name || "file";
		const ext = (name.split(".").pop() || "").toUpperCase();
		return `<a class="bwa-file" href="${url}" target="_blank" rel="noopener">
			<span class="bwa-file-icon">${ext === "PDF" ? "📕" : "📄"}</span>
			<span><div class="bwa-file-name">${esc(decodeURIComponent(name))}</div>
			<div class="bwa-file-sub">${esc(ext)} · ${__w("Open / download")}</div></span></a>`;
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
		if (s === "read") return `<span class="bwa-ticks read" title="${__w("Read")}">✓✓</span>`;
		if (s === "delivered") return `<span class="bwa-ticks" title="${__w("Delivered")}">✓✓</span>`;
		if (s === "failed") return `<span class="bwa-ticks failed" title="${__w("Failed to send")}">⚠</span>`;
		return `<span class="bwa-ticks" title="${__w("Sent")}">✓</span>`;
	}

	// ----------------------------------------------------------- time helpers
	moment(dt) {
		if (!dt) return null;
		const m = moment(frappe.datetime.convert_to_user_tz ? frappe.datetime.convert_to_user_tz(dt) : dt);
		return m.locale((frappe.boot && frappe.boot.lang) || "en");
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
		if (m.isSame(now.clone().subtract(1, "day"), "day")) return __w("Yesterday");
		if (m.isAfter(now.clone().subtract(6, "day"))) return m.format("dddd");
		return m.format("YYYY/MM/DD");
	}
	day_label(dt) {
		const m = this.moment(dt);
		if (!m) return "";
		const now = moment();
		if (m.isSame(now, "day")) return __w("Today");
		if (m.isSame(now.clone().subtract(1, "day"), "day")) return __w("Yesterday");
		return m.format("dddd YYYY/MM/DD");
	}

	// ------------------------------------------------------- reply / popups
	set_reply(m) {
		this.reply_to = m && m.message_id ? m : null;
		const $bar = this.$chat.find(".bwa-replybar");
		if (!this.reply_to) return $bar.hide().empty();
		const who = m.type === "Outgoing" ? (m.sender || __w("Us")) : __w("Customer");
		$bar.html(`<div class="bwa-quote"><b>↩ ${__w("Reply to {0}", [esc(who)])}</b><div>${esc(String(m.message || `[${m.content_type}]`).slice(0, 120))}</div></div>
			<span class="bwa-x" title="${__w("Cancel")}">✕</span>`).show();
		this.$input.trigger("focus");
	}

	toggle_emoji($input) {
		$input = $input || this.$input;
		if (this.$popup.is(":visible") && this.popup_kind === "emoji") return this.$popup.hide();
		this.popup_kind = "emoji";
		this.$popup.html(`<div class="bwa-emojis">${EMOJIS.map((e) => `<span>${e}</span>`).join("")}</div>`).show();
		this.$popup.find("span").on("click", (e) => {
			const el = $input.get(0);
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
			this.$popup.html(`<div class="text-muted small p-2">${this.ctx.quick_replies.length
				? __w("No matching quick replies")
				: __w("No quick replies - add them from the menu ⋯ > Quick Replies")}</div>`).show();
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
				frappe.show_alert({ message: __w("Sending the file..."), indicator: "blue" });
				const m = await frappe.xcall(`${BWA}.send_file`, {
					number: this.number, file_url: file.file_url, caption,
					reply_to: this.reply_to ? this.reply_to.message_id : null,
				});
				if (caption) this.$input.val("").trigger("input");
				this.set_reply(null);
				this.push_local(m);
				frappe.show_alert({ message: __w("File sent") + " ✅", indicator: "green" });
			},
		});
	}

	// voice notes ---------------------------------------------------------
	async start_recording() {
		if (!navigator.mediaDevices || !window.MediaRecorder) {
			frappe.msgprint(__w("This browser cannot record audio."));
			return;
		}
		let stream;
		try {
			stream = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch (e) {
			frappe.msgprint(__w("Microphone access was not allowed. Enable it for this site in the browser settings."));
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
			frappe.show_alert({ message: __w("Sending the voice message..."), indicator: "blue" });
			try {
				const file_url = await this.upload_blob(blob, `voice-${Date.now()}.${ext}`);
				const m = await frappe.xcall(`${BWA}.send_voice`, {
					number: this.number, file_url,
					reply_to: this.reply_to ? this.reply_to.message_id : null,
				});
				this.set_reply(null);
				this.push_local(m);
			} catch (e) {
				console.error(e);
				frappe.msgprint(__w("The voice message could not be sent."));
			}
		};
		r.rec.stop();
	}

	async upload_blob(blob, filename) {
		const fd = new FormData();
		fd.append("file", blob, filename);
		fd.append("is_private", "1");
		const res = await fetch("/api/method/upload_file", {
			method: "POST", body: fd, headers: { "X-Frappe-CSRF-Token": frappe.csrf_token },
		});
		const j = await res.json();
		if (!res.ok) throw new Error(j.exception || res.statusText);
		return j.message.file_url;
	}

	push_local(m) {
		if (!this.messages.find((x) => x.name === m.name)) this.messages.push(m);
		this.render_messages();
		this.scroll_bottom();
		this.load_list(true);
	}

	// ======================================================== team chat
	async load_channels(quiet = false) {
		try {
			this.channels = await frappe.xcall(`${STAFF}.get_channels`);
		} catch (e) {
			if (!quiet) console.error(e);
			return;
		}
		this.render_channels();
	}

	render_channels() {
		const total = this.channels.reduce((s, c) => s + (c.unread || 0), 0);
		this.$root.find(".bwa-staff-count").text(total).toggle(total > 0);
		if (!this.channels.length) {
			this.$channels.html(`<div class="bwa-empty">${__w("No team chats yet. Add members to a team, or message a colleague.")}</div>`);
			return;
		}
		this.$channels.html(this.channels.map((c) => `
			<div class="bwa-item ${c.unread ? "unread" : ""} ${c.channel === this.channel ? "active" : ""}" data-channel="${esc(c.channel)}">
				<div class="bwa-avatar ${c.kind === "team" ? "team" : ""}">${c.kind === "team" ? "👥" : esc((c.title || "?").charAt(0))}</div>
				<div class="bwa-item-body">
					<div class="bwa-row">
						<div class="bwa-title">${esc(c.title)}${c.kind === "team" ? `<span class="bwa-tag">${__w("Team")}</span>` : ""}</div>
						<div class="bwa-time">${c.last_at ? this.short_time(c.last_at) : ""}</div>
					</div>
					<div class="bwa-row">
						<div class="bwa-preview">${c.last ? `${c.last_by ? esc(c.last_by) + ": " : ""}${esc(c.last)}` : `<span class="text-muted">${__w("No messages yet")}</span>`}</div>
						${c.unread ? `<span class="bwa-count">${c.unread}</span>` : ""}
					</div>
				</div>
			</div>`).join(""));
	}

	new_direct_dialog() {
		const d = new frappe.ui.Dialog({
			title: __w("Message a colleague"),
			fields: [{ fieldname: "user", fieldtype: "Autocomplete", label: __w("Colleague"), reqd: 1,
				options: (this.ctx.users || []).filter((u) => u.user !== frappe.session.user).map((u) => ({ value: u.user, label: u.name })) }],
			primary_action_label: __w("Open"),
			primary_action: async (v) => {
				const r = await frappe.xcall(`${STAFF}.open_direct`, { user: v.user });
				d.hide();
				this.open_channel(r.channel);
			},
		});
		d.show();
	}

	async open_channel(channel) {
		if (!channel) return;
		this.channel = channel;
		this.number = null;
		this.$root.addClass("chat-open");
		this.$channels.find(".bwa-item").removeClass("active");
		this.$channels.find(`.bwa-item[data-channel="${CSS.escape(channel)}"]`).addClass("active").removeClass("unread")
			.find(".bwa-count").remove();
		this.render_staff_shell();
		try {
			this.staff = await frappe.xcall(`${STAFF}.get_staff_messages`, { channel });
		} catch (e) {
			this.$msgs.html(`<div class="bwa-empty">${__w("You are not a member of this chat")}</div>`);
			return;
		}
		if (this.channel !== channel) return;
		this.render_staff_head();
		this.render_staff_messages();
		this.scroll_bottom();
		this.load_channels(true);
	}

	render_staff_shell() {
		this.$chat.html(`
			<div class="bwa-head"></div>
			<div class="bwa-msgs bwa-staff-msgs"><div class="text-muted text-center p-4">${__w("Loading...")}</div></div>
			<div class="bwa-popup" style="display:none"></div>
			<div class="bwa-compose">
				<button class="bwa-icon-btn bwa-emoji-btn" title="${__w("Emoji")}">😊</button>
				<button class="bwa-icon-btn bwa-attach" title="${__w("Attach a file")}">📎</button>
				<textarea class="form-control" rows="1" placeholder="${__w("Message your colleagues (not sent to customers)...")}"></textarea>
				<button class="bwa-icon-btn bwa-send" title="${__w("Send")}">➤</button>
			</div>`);
		this.$msgs = this.$chat.find(".bwa-msgs");
		this.$popup = this.$chat.find(".bwa-popup");
		const $input = this.$chat.find("textarea");
		this.$staff_input = $input;
		this.$chat.find(".bwa-send").on("click", () => this.send_staff());
		this.$chat.find(".bwa-emoji-btn").on("click", () => this.toggle_emoji($input));
		this.$chat.find(".bwa-attach").on("click", () => this.staff_file());
		$input.on("input", () => this.autosize($input));
		$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey && !frappe.is_mobile()) {
				e.preventDefault();
				this.send_staff();
			}
		});
		this.$msgs.on("click", ".bwa-ref-open", (e) => {
			e.preventDefault();
			const num = $(e.currentTarget).attr("data-number");
			this.set_mode("customers");
			this.open(num);
		});
		this.$msgs.on("click", ".bwa-more button", () => this.load_older_staff());
	}

	render_staff_head() {
		const s = this.staff;
		const members = (s.members || []).map((m) => esc(m.name)).join(", ");
		this.$chat.find(".bwa-head").html(`
			<div class="bwa-head-top">
				<button class="btn btn-xs btn-default bwa-back">${this.back_label()}</button>
				<div style="flex:1;min-width:0">
					<div class="bwa-head-name">${s.kind === "team" ? "👥 " : "👤 "}${esc(s.title)}</div>
					<div class="bwa-head-sub text-muted small">${s.kind === "team" ? `${__w("Members")}: ${members}` : __w("Direct messages")}</div>
				</div>
			</div>`);
		this.$chat.find(".bwa-back").on("click", () => this.close_chat());
	}

	render_staff_messages() {
		const s = this.staff;
		const parts = [];
		if (s.has_more) parts.push(`<div class="bwa-more"><button class="btn btn-xs btn-default">${__w("Load older messages")}</button></div>`);
		let day = null;
		for (const m of s.messages) {
			const d = (m.creation || "").slice(0, 10);
			if (d !== day) {
				day = d;
				parts.push(`<div class="bwa-day"><span>${this.day_label(m.creation)}</span></div>`);
			}
			let body = s.kind === "team" && !m.mine ? `<div class="bwa-sender">${esc(m.sender)}</div>` : "";
			if (m.message) body += `<div class="bwa-text">${this.linkify(m.message)}</div>`;
			if (m.attach) {
				const url = encodeURI(m.attach);
				body += /\.(png|jpe?g|gif|webp)$/i.test(m.attach)
					? `<div class="bwa-media"><img src="${url}" loading="lazy" alt=""></div>`
					: this.file_html(url, m.file_name);
			}
			if (m.ref_number) {
				body += `<a href="#" class="bwa-ref bwa-ref-open" data-number="${esc(m.ref_number)}">💬 ${__w("Customer conversation")}: ${esc(m.ref_title || "+" + m.ref_number)}</a>`;
			}
			parts.push(`<div class="bwa-msg ${m.mine ? "out" : "in"}" data-name="${esc(m.name)}">
				<div class="bwa-bubble" dir="auto">${body}<div class="bwa-meta"><span>${this.clock(m.creation)}</span></div></div></div>`);
		}
		if (!s.messages.length) parts.push(`<div class="bwa-empty">${__w("No messages yet - say hello 👋")}</div>`);
		this.$msgs.html(parts.join(""));
	}

	async load_older_staff() {
		const s = this.staff;
		if (!s || !s.messages.length) return;
		const el = this.$msgs.get(0);
		const before_height = el.scrollHeight;
		const r = await frappe.xcall(`${STAFF}.get_staff_messages`, { channel: s.channel, before: s.messages[0].creation });
		s.messages = r.messages.concat(s.messages);
		s.has_more = r.has_more;
		this.render_staff_messages();
		el.scrollTop = el.scrollHeight - before_height;
	}

	async send_staff(extra = {}) {
		const $input = this.$staff_input;
		const text = ($input.val() || "").trim();
		if ((!text && !extra.attach) || this.sending) return;
		this.sending = true;
		try {
			const m = await frappe.xcall(`${STAFF}.send_staff_message`, { channel: this.channel, message: text, ...extra });
			$input.val("").trigger("input");
			this.$popup.hide();
			this.append_staff(m);
		} finally {
			this.sending = false;
		}
	}

	staff_file() {
		new frappe.ui.FileUploader({
			allow_multiple: false,
			make_attachments_public: false,
			on_success: (file) => this.send_staff({ attach: file.file_url }),
		});
	}

	append_staff(m) {
		if (!this.staff || m.channel !== this.staff.channel) return;
		if (!this.staff.messages.find((x) => x.name === m.name)) this.staff.messages.push(m);
		this.render_staff_messages();
		this.scroll_bottom();
		this.load_channels(true);
	}

	// ------------------------------------------------------------ realtime
	bind_realtime() {
		const refresh_list = frappe.utils.debounce(() => this.load_list(true), 800);
		const refresh_channels = frappe.utils.debounce(() => this.load_channels(true), 500);

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
					this.$msgs.html(`<div class="bwa-empty">${__w("This conversation is no longer assigned to you")}</div>`);
				});
			}
			if (data.kind === "read" || data.kind === "note") return this.load_messages({ keep_scroll: true });
			if (data.name) this.upsert_message(data.name);
			else this.load_messages({ keep_scroll: true });
		});

		frappe.realtime.on("butler_wa_assigned", () => refresh_list());

		frappe.realtime.on("butler_wa_staff", async (data) => {
			refresh_channels();
			if (this.channel && data.channel === this.channel && data.by !== frappe.session.user) {
				const r = await frappe.xcall(`${STAFF}.get_staff_messages`, { channel: this.channel });
				if (this.channel !== data.channel) return;
				const el = this.$msgs.get(0);
				const at_bottom = el.scrollTop + el.clientHeight > el.scrollHeight - 120;
				this.staff = r;
				this.render_staff_messages();
				if (at_bottom) this.scroll_bottom();
			}
		});

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
