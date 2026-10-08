// Butler - WhatsApp inbox (Arabic, RTL, works on phones).
// Server: butler_whatsapp/inbox.py

frappe.pages["whatsapp-inbox"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: "واتساب", single_column: true });
	wrapper.butler_inbox = new ButlerInbox(page, wrapper);
};

frappe.pages["whatsapp-inbox"].on_page_show = function (wrapper) {
	wrapper.butler_inbox && wrapper.butler_inbox.on_show();
};

const BWA = "butler_whatsapp.inbox";
const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));

class ButlerInbox {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.number = null;
		this.info = null;
		this.messages = [];
		this.has_more = false;
		this.conversations = [];
		this.search = "";
		this.list_offset = 0;
		this.list_done = false;
		this.make();
		this.bind_realtime();
		this.load_list();
		setInterval(() => this.is_visible() && this.load_list(true), 60000);
	}

	// ------------------------------------------------------------ layout
	make() {
		this.page.set_secondary_action("تحديث", () => this.refresh_all(), "refresh");
		this.page.add_menu_item("تفعيل الإشعارات على هذا الجهاز", () => window.butler_wa && window.butler_wa.enablePush());
		this.page.add_menu_item("واجهة المحادثة القديمة", () => $("#chat-bubble").trigger("click"));
		this.page.add_menu_item("كل رسائل واتساب", () => frappe.set_route("List", "WhatsApp Message"));

		this.$root = $(`
			<div class="bwa">
				<div class="bwa-list">
					<div class="bwa-search">
						<input type="search" class="form-control" placeholder="بحث بالاسم أو الرقم أو العميل...">
					</div>
					<div class="bwa-items"></div>
				</div>
				<div class="bwa-chat">
					<div class="bwa-placeholder">
						<div style="font-size:48px">💬</div>
						<div>اختر محادثة من القائمة</div>
					</div>
				</div>
			</div>`).appendTo(this.page.main);

		this.$items = this.$root.find(".bwa-items");
		this.$chat = this.$root.find(".bwa-chat");

		this.$root.find(".bwa-search input").on("input", frappe.utils.debounce((e) => {
			this.search = e.target.value.trim();
			this.load_list();
		}, 350));

		this.$items.on("scroll", () => {
			const el = this.$items.get(0);
			if (!this.list_done && !this.list_loading && el.scrollTop + el.clientHeight > el.scrollHeight - 80) {
				this.load_list(false, true);
			}
		});
		this.$items.on("click", ".bwa-item", (e) => this.open($(e.currentTarget).attr("data-number")));
	}

	is_visible() {
		return frappe.get_route_str() === "whatsapp-inbox" && !document.hidden;
	}

	on_show() {
		const params = new URLSearchParams(location.search);
		const number = (frappe.route_options && frappe.route_options.number) || params.get("number");
		frappe.route_options = null;
		if (number && number !== this.number) this.open(number);
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
			const rows = await frappe.xcall(`${BWA}.get_conversations`, { search: this.search, offset, limit: 60 });
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
			this.$items.html(`<div class="bwa-empty">${this.search ? "لا توجد نتائج" : "لا توجد محادثات بعد"}</div>`);
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
		let sub = "";
		if (last.type === "Incoming" && last.read_by && !c.unread) {
			sub = `<div class="bwa-sub">👁 قرأها: ${esc(last.read_by)}</div>`;
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
					${sub}
				</div>
			</div>`;
	}

	// ------------------------------------------------------ conversation
	async open(number) {
		number = String(number || "").replace(/[^0-9]/g, "");
		if (!number) return;
		this.number = number;
		this.messages = [];
		this.info = null;
		this.$root.addClass("chat-open");
		this.$items.find(".bwa-item").removeClass("active");
		this.$items.find(`.bwa-item[data-number="${number}"]`).addClass("active").removeClass("unread")
			.find(".bwa-count").remove();
		this.render_chat_shell();
		await this.load_messages({ mark_read: 1 });
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
			<div class="bwa-compose">
				<button class="bwa-icon-btn bwa-attach" title="إرسال ملف">📎</button>
				<textarea class="form-control" rows="1" placeholder="اكتب رسالة..."></textarea>
				<button class="bwa-icon-btn bwa-send" title="إرسال">➤</button>
			</div>`);
		this.$msgs = this.$chat.find(".bwa-msgs");
		this.$input = this.$chat.find("textarea");
		this.$chat.find(".bwa-send").on("click", () => this.send_text());
		this.$chat.find(".bwa-attach").on("click", () => this.pick_file());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey && !frappe.is_mobile()) {
				e.preventDefault();
				this.send_text();
			}
		});
		this.$input.on("input", () => {
			const el = this.$input.get(0);
			el.style.height = "auto";
			el.style.height = Math.min(el.scrollHeight, 140) + "px";
		});
		this.$msgs.on("click", ".bwa-more button", () => this.load_older());
		this.$msgs.on("click", ".bwa-media img", (e) => window.open(e.currentTarget.src, "_blank"));
	}

	async load_messages({ mark_read = 0, keep_scroll = false } = {}) {
		const number = this.number;
		const r = await frappe.xcall(`${BWA}.get_messages`, { number, mark_read });
		if (number !== this.number) return;
		const el = this.$msgs.get(0);
		const at_bottom = !keep_scroll || el.scrollTop + el.clientHeight > el.scrollHeight - 60;
		this.messages = r.messages;
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
		const m = await frappe.xcall(`${BWA}.get_message`, { name });
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
	set_info(info) {
		this.info = info;
		const title = info.customer_name || info.lead_name ||
			(info.contact_name && info.contact_name.replace("+", "") !== info.number ? info.contact_name : null) ||
			info.profile_name || "+" + info.number;
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

		this.$chat.find(".bwa-head").html(`
			<div class="bwa-head-top">
				<button class="btn btn-xs btn-default bwa-back">→ رجوع</button>
				<div style="flex:1;min-width:0">
					<div class="bwa-head-name">${esc(title)}</div>
					<div class="bwa-head-num">+${esc(info.number)}${info.profile_name && info.profile_name !== title ? ` · ${esc(info.profile_name)}` : ""}</div>
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
		$head.find("[data-unlink]").on("click", (e) => this.unlink($(e.currentTarget).attr("data-unlink")));
		$head.find(".bwa-link-match").on("click", (e) => {
			e.preventDefault();
			const $a = $(e.currentTarget);
			this.link($a.attr("data-doctype"), $a.attr("data-name"));
		});

		const $w = this.$chat.find(".bwa-window");
		const can_send = info.can_send && info.in_window;
		if (!info.in_window) {
			$w.html(`⏰ لم يرسل العميل أي رسالة خلال آخر 24 ساعة — واتساب لا يسمح بإرسال رسالة حرة الآن.
				يمكن إرسال قالب معتمد فقط، أو انتظر حتى يراسلك العميل.`).show();
		} else if (!info.can_send) {
			$w.html("ليست لديك صلاحية الإرسال.").show();
		} else {
			$w.hide();
		}
		this.$chat.find(".bwa-compose button, .bwa-compose textarea").prop("disabled", !can_send);
	}

	default_name() {
		const i = this.info || {};
		return i.profile_name || (i.contact_name && i.contact_name.replace("+", "") !== i.number ? i.contact_name : "") || "";
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

	// ---------------------------------------------------------- messages
	render_messages() {
		const parts = [];
		if (this.has_more) parts.push(`<div class="bwa-more"><button class="btn btn-xs btn-default">تحميل الرسائل الأقدم</button></div>`);
		let day = null;
		for (const m of this.messages) {
			const d = (m.creation || "").slice(0, 10);
			if (d !== day) {
				day = d;
				parts.push(`<div class="bwa-day"><span>${this.day_label(m.creation)}</span></div>`);
			}
			parts.push(this.bubble(m));
		}
		if (!this.messages.length) parts.push(`<div class="bwa-empty">لا توجد رسائل</div>`);
		this.$msgs.html(parts.join(""));
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
		let body = "";
		if (m.message_type === "Template") body += `<div class="bwa-tpl">📋 قالب${m.template ? ": " + esc(m.template) : ""}</div>`;
		body += this.content(m);
		if (m.reference_doctype && m.reference_name) {
			body += `<div class="bwa-ref">🔗 <a href="/desk/${frappe.router.slug(m.reference_doctype)}/${encodeURIComponent(m.reference_name)}">${esc(__(m.reference_doctype))} ${esc(m.reference_name)}</a></div>`;
		}
		return `<div class="bwa-msg ${out ? "out" : "in"}" data-name="${esc(m.name)}">
			<div class="bwa-bubble">${body}<div class="bwa-meta">${meta}</div></div></div>`;
	}

	content(m) {
		const type = m.content_type || "text";
		const url = m.attach ? encodeURI(m.attach) : null;
		const caption = m.message ? `<div class="bwa-text">${this.linkify(m.message)}</div>` : "";
		if (type === "text" || type === "button" || type === "interactive") {
			return `<div class="bwa-text">${this.linkify(m.message) || "&nbsp;"}</div>`;
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

	// ------------------------------------------------------------ sending
	async send_text() {
		const text = (this.$input.val() || "").trim();
		if (!text || this.sending) return;
		this.sending = true;
		this.$chat.find(".bwa-send").prop("disabled", true);
		try {
			const m = await frappe.xcall(`${BWA}.send_text`, { number: this.number, message: text });
			this.$input.val("").trigger("input");
			this.push_local(m);
		} finally {
			this.sending = false;
			this.$chat.find(".bwa-send").prop("disabled", !(this.info && this.info.in_window));
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
				});
				if (caption) this.$input.val("").trigger("input");
				this.push_local(m);
				frappe.show_alert({ message: "تم إرسال الملف ✅", indicator: "green" });
			},
		});
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
			// new message re-opens the 24h window
			if (this.info && !this.info.in_window) this.reload_info();
		});

		frappe.realtime.on("butler_wa_update", (data) => {
			refresh_list();
			if (!this.number || data.number !== this.number) return;
			if (data.kind === "party") return this.reload_info();
			if (data.kind === "read") return this.load_messages({ keep_scroll: true });
			if (data.name) this.upsert_message(data.name);
		});

		document.addEventListener("visibilitychange", () => {
			if (!document.hidden && this.number && frappe.get_route_str() === "whatsapp-inbox") {
				this.load_messages({ mark_read: 1, keep_scroll: true });
			}
		});
	}

	update_launcher_badge() {
		window.butler_wa && window.butler_wa.refreshBadge && window.butler_wa.refreshBadge();
	}
}
