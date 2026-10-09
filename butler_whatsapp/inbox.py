"""Butler WhatsApp inbox (Desk page "whatsapp-inbox").

- Conversations are grouped by the customer's number straight from
  "WhatsApp Message", so every message shows up (templates and PDFs too).
- Read tracking: the first user who opens a conversation is stored on each
  unread incoming message (butler_read_by / butler_read_at).
- Files are sent by uploading to Meta and sending the media id, so private
  files never need a public URL.
- A conversation can be linked to a Lead or Customer. Existing parties are
  found automatically by phone number (last 9 digits).
- Conversations are assigned to staff; only supervisors see all of them
  (see access.py). Each has a status (Open / Closed) and internal notes.
"""

from __future__ import annotations

import mimetypes

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_fullname, now_datetime, strip_html

from butler_whatsapp import access
from butler_whatsapp.api import _account, _client, _last_incoming
from butler_whatsapp.meta import (
	WhatsAppError,
	count_body_variables,
	media_kind,
	media_message,
	normalize_msisdn,
	phone_tail,
	render_template,
	template_message,
	text_message,
	with_reply,
	within_service_window,
)

MSG = "WhatsApp Message"
CONTACT = "WhatsApp Contact"
UPDATE_EVENT = "butler_wa_update"
LEAD_SOURCE = "WhatsApp"
MATCH_CACHE_TTL = 600

# SQL expression: the customer's number of a message, without "+"
_NUM = "REPLACE(IF(m.type = 'Incoming', m.`from`, m.`to`), '+', '')"


# ------------------------------------------------------------- helpers


def _require(ptype: str = "read"):
	frappe.has_permission(MSG, ptype, throw=True)


def _num(number: str) -> str:
	num = normalize_msisdn(number)
	if not num:
		frappe.throw(_("Invalid mobile number: {0}").format(number))
	return num


def _variants(num: str) -> list[str]:
	return [num, "+" + num]


def _contact(num: str, create: bool = False, display: str | None = None):
	name = frappe.db.get_value(CONTACT, {"mobile_no": ["in", _variants(num)]}, "name")
	if name or not create:
		return name
	doc = frappe.get_doc(
		{"doctype": CONTACT, "mobile_no": num, "contact_name": display or num, "is_read": 1}
	).insert(ignore_permissions=True)
	return doc.name


_names: dict[str, str] = {}


def _user_name(user: str | None) -> str | None:
	if not user:
		return None
	if user not in _names:
		_names[user] = get_fullname(user) or user
	return _names[user]


def _sender(owner: str | None) -> str:
	if not owner or owner in ("Administrator", "Guest"):
		return _("System")
	return _user_name(owner)


def _preview(row) -> str:
	ctype = row.get("content_type") or "text"
	text = strip_html(row.get("message") or "").strip()
	if row.get("message_type") == "Template" and not text:
		text = _("Template: {0}").format(row.get("template") or "")
	if ctype == "text":
		return text[:120]
	labels = {"image": "📷", "video": "🎥", "audio": "🎤", "document": "📄", "location": "📍",
		"reaction": "👍", "contacts": "👤"}
	return f"{labels.get(ctype, '📎')} {text}".strip()[:120]


def publish(num: str, kind: str, name: str | None = None, extra_users: list[str] | None = None):
	"""Tell the inboxes that may show conversation `num` that it changed."""
	data = {"number": num, "kind": kind, "name": name}
	for user in _watchers(num) | set(extra_users or []):
		frappe.publish_realtime(UPDATE_EVENT, data, user=user, after_commit=True)


def _watchers(num: str) -> set[str]:
	from butler_whatsapp.realtime import _agents

	if not access.restricted():
		return set(_agents())
	users = set(access.supervisors())
	who = access.assignee(num)
	if who:
		users.add(who)
	return users


def _require_number(number: str, ptype: str = "read") -> str:
	_require(ptype)
	num = _num(number)
	access.check_access(num)
	return num


# ---------------------------------------------------------- party matching


def match_parties(num: str) -> list[dict]:
	"""Customers, Leads and Contacts whose phone ends with the same 9 digits."""
	tail = phone_tail(num)
	if not tail:
		return []
	like = "%" + tail[-4:]
	norm = "RIGHT(REGEXP_REPLACE({0}, '[^0-9]', ''), 9) = %(tail)s AND {0} LIKE %(like)s"
	args = {"tail": tail, "like": like}
	found: dict[tuple, dict] = {}

	def add(doctype, name, title, via):
		found.setdefault((doctype, name), {"doctype": doctype, "name": name, "title": title or name, "via": via})

	for r in frappe.db.sql(
		f"SELECT name, customer_name FROM `tabCustomer` WHERE disabled = 0 AND {norm.format('mobile_no')}",
		args, as_dict=True,
	):
		add("Customer", r.name, r.customer_name, "Customer")

	for r in frappe.db.sql(
		f"""SELECT name, lead_name, company_name, status FROM `tabLead`
		WHERE ({norm.format('mobile_no')}) OR ({norm.format('phone')}) OR ({norm.format('whatsapp_no')})""",
		args, as_dict=True,
	):
		add("Lead", r.name, r.lead_name or r.company_name, "Lead")

	for r in frappe.db.sql(
		f"""SELECT DISTINCT dl.link_doctype, dl.link_name, c.name AS contact, c.full_name
		FROM `tabContact Phone` cp
		JOIN `tabContact` c ON c.name = cp.parent
		LEFT JOIN `tabDynamic Link` dl ON dl.parenttype = 'Contact' AND dl.parent = c.name
			AND dl.link_doctype IN ('Customer', 'Lead')
		WHERE {norm.format('cp.phone')}""",
		args, as_dict=True,
	):
		if r.link_doctype:
			title = frappe.db.get_value(
				r.link_doctype, r.link_name, "customer_name" if r.link_doctype == "Customer" else "lead_name"
			)
			add(r.link_doctype, r.link_name, title, "Contact")
		else:
			add("Contact", r.contact, r.full_name, "Contact")

	order = {"Customer": 0, "Lead": 1, "Contact": 2}
	return sorted(found.values(), key=lambda p: (order[p["doctype"]], p["name"]))


def _cached_matches(num: str) -> list[dict]:
	key = f"butler_wa_match:{num}"
	cached = frappe.cache.get_value(key)
	if cached is None:
		cached = match_parties(num)
		frappe.cache.set_value(key, cached, expires_in_sec=MATCH_CACHE_TTL)
	return cached


def _auto_link(contact: str, matches: list[dict]) -> dict:
	"""Link the conversation when exactly one Customer (or else one Lead) matches."""
	customers = [m for m in matches if m["doctype"] == "Customer"]
	leads = [m for m in matches if m["doctype"] == "Lead"]
	values = {}
	if len(customers) == 1:
		values["butler_customer"] = customers[0]["name"]
	if len(leads) == 1:
		values["butler_lead"] = leads[0]["name"]
	if values:
		frappe.db.set_value(CONTACT, contact, values, update_modified=False)
	return values


def _party(contact: dict | None) -> dict:
	out = {"customer": None, "customer_name": None, "lead": None, "lead_name": None}
	if not contact:
		return out
	if contact.get("butler_customer"):
		out["customer"] = contact["butler_customer"]
		out["customer_name"] = frappe.db.get_value("Customer", out["customer"], "customer_name")
	if contact.get("butler_lead"):
		out["lead"] = contact["butler_lead"]
		lead = frappe.db.get_value("Lead", out["lead"], ["lead_name", "company_name", "status"], as_dict=True)
		if lead:
			out["lead_name"] = lead.lead_name or lead.company_name
			out["lead_status"] = lead.status
	return out


# ------------------------------------------------------------ conversations


@frappe.whitelist()
def get_conversations(
	search: str | None = None, limit: int = 60, offset: int = 0, view: str | None = None
) -> list[dict]:
	"""view: all (open) | unread | mine | unassigned | closed"""
	_require()
	limit = min(cint(limit) or 60, 200)
	offset = cint(offset)
	user = frappe.session.user
	args = {"limit": limit, "offset": offset, "user": user}
	where = []
	if not access.is_supervisor(user):
		where.append("c.butler_assigned_to = %(user)s")
	view = view or "all"
	if view == "closed":
		where.append("c.butler_status = 'Closed'")
	else:
		where.append("IFNULL(c.butler_status, 'Open') != 'Closed'")
	if view == "unread":
		where.append("t.unread > 0")
	elif view == "mine":
		where.append("c.butler_assigned_to = %(user)s")
	elif view == "unassigned":
		where.append("IFNULL(c.butler_assigned_to, '') = ''")
	if search:
		args["q"] = f"%{search.strip()}%"
		digits = "".join(ch for ch in search if ch.isdigit()).lstrip("0")
		parts = ["c.contact_name LIKE %(q)s", "cu.customer_name LIKE %(q)s", "l.lead_name LIKE %(q)s",
			"t.profile_name LIKE %(q)s"]
		if len(digits) >= 3:
			args["d"] = f"%{digits[-9:]}%"
			parts.append("t.num LIKE %(d)s")
		where.append("(" + " OR ".join(parts) + ")")
	cond = "WHERE " + " AND ".join(where)

	rows = frappe.db.sql(
		f"""
		SELECT t.num, t.last_at, t.unread, t.profile_name,
			c.name AS contact, c.contact_name, c.butler_customer, c.butler_lead, c.butler_no_auto_link,
			c.butler_assigned_to, c.butler_status,
			cu.customer_name, l.lead_name
		FROM (
			SELECT {_NUM} AS num, MAX(m.creation) AS last_at,
				SUM(m.type = 'Incoming' AND m.butler_read_at IS NULL) AS unread,
				MAX(IF(m.type = 'Incoming', m.profile_name, NULL)) AS profile_name
			FROM `tabWhatsApp Message` m
			WHERE IFNULL(IF(m.type = 'Incoming', m.`from`, m.`to`), '') != ''
			GROUP BY num
		) t
		LEFT JOIN (
			SELECT REPLACE(mobile_no, '+', '') AS num, MIN(name) AS name
			FROM `tabWhatsApp Contact` GROUP BY REPLACE(mobile_no, '+', '')
		) cc ON cc.num = t.num
		LEFT JOIN `tabWhatsApp Contact` c ON c.name = cc.name
		LEFT JOIN `tabCustomer` cu ON cu.name = c.butler_customer
		LEFT JOIN `tabLead` l ON l.name = c.butler_lead
		{cond}
		ORDER BY t.last_at DESC
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		args,
		as_dict=True,
	)
	if not rows:
		return []

	last = {
		r.num: r
		for r in frappe.db.sql(
			f"""
			SELECT {_NUM} AS num, m.name, m.type, m.message, m.content_type, m.message_type, m.template,
				m.status, m.owner, m.creation, m.butler_read_by, m.butler_read_at
			FROM `tabWhatsApp Message` m
			WHERE ({_NUM}, m.creation) IN ({", ".join(["(%s, %s)"] * len(rows))})
			ORDER BY m.creation
			""",
			[v for r in rows for v in (r.num, r.last_at)],
			as_dict=True,
		)
	}

	out = []
	for r in rows:
		msg = last.get(r.num) or {}
		matched = None
		if not r.butler_customer and not r.butler_lead:
			matches = _cached_matches(r.num)
			if r.contact and matches and not r.butler_no_auto_link:
				linked = _auto_link(r.contact, matches)
				if linked.get("butler_customer"):
					r.butler_customer = linked["butler_customer"]
					r.customer_name = frappe.db.get_value("Customer", r.butler_customer, "customer_name")
				if linked.get("butler_lead"):
					r.butler_lead = linked["butler_lead"]
					r.lead_name = frappe.db.get_value("Lead", r.butler_lead, "lead_name")
			if not r.butler_customer and not r.butler_lead and matches:
				matched = matches[0]
		title = r.customer_name or r.lead_name or (
			r.contact_name if r.contact_name and r.contact_name.lstrip("+") != r.num else None
		) or r.profile_name or ("+" + r.num)
		out.append(
			{
				"number": r.num,
				"title": title,
				"profile_name": r.profile_name,
				"customer": r.butler_customer,
				"lead": r.butler_lead,
				"suggestion": matched,
				"assigned_to": r.butler_assigned_to,
				"assigned_name": _user_name(r.butler_assigned_to),
				"status": r.butler_status or "Open",
				"unread": cint(r.unread),
				"last_at": str(r.last_at),
				"last": {
					"type": msg.get("type"),
					"preview": _preview(msg) if msg else "",
					"status": msg.get("status"),
					"sender": _sender(msg.get("owner")) if msg.get("type") == "Outgoing" else None,
					"read_by": _user_name(msg.get("butler_read_by")),
					"read_at": str(msg.get("butler_read_at")) if msg.get("butler_read_at") else None,
				},
			}
		)
	return out


@frappe.whitelist()
def get_unread_total() -> int:
	if not frappe.has_permission(MSG, "read"):
		return 0
	if access.is_supervisor():
		return cint(
			frappe.db.sql(
				"SELECT COUNT(*) FROM `tabWhatsApp Message` WHERE type = 'Incoming' AND butler_read_at IS NULL"
			)[0][0]
		)
	return cint(
		frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabWhatsApp Message` m
			WHERE m.type = 'Incoming' AND m.butler_read_at IS NULL AND REPLACE(m.`from`, '+', '') IN
				(SELECT REPLACE(mobile_no, '+', '') FROM `tabWhatsApp Contact` WHERE butler_assigned_to = %s)""",
			(frappe.session.user,),
		)[0][0]
	)


def _message_row(m) -> dict:
	return {
		"name": m.name,
		"type": m.type,
		"message": m.message or "",
		"content_type": m.content_type or "text",
		"message_type": m.message_type,
		"template": m.template,
		"attach": m.attach,
		"file_name": (m.attach or "").rsplit("/", 1)[-1] or None,
		"status": m.status,
		"creation": str(m.creation),
		"sender": _sender(m.owner) if m.type == "Outgoing" else (m.profile_name or None),
		"read_by": _user_name(m.butler_read_by),
		"read_at": str(m.butler_read_at) if m.butler_read_at else None,
		"is_reply": cint(m.is_reply),
		"reply_to": m.reply_to_message_id,
		"message_id": m.message_id,
		"reference_doctype": m.reference_doctype,
		"reference_name": m.reference_name,
	}


_FIELDS = [
	"name", "type", "message", "content_type", "message_type", "template", "attach", "status", "owner",
	"creation", "profile_name", "butler_read_by", "butler_read_at", "is_reply", "reply_to_message_id",
	"message_id", "reference_doctype", "reference_name",
]


@frappe.whitelist()
def get_messages(number: str, before: str | None = None, limit: int = 40, mark_read: int = 1) -> dict:
	"""Messages of one conversation (newest page first) + conversation info."""
	num = _require_number(number)
	limit = min(cint(limit) or 40, 200)
	conditions = "(`from` IN %(v)s AND type = 'Incoming') OR (`to` IN %(v)s AND type = 'Outgoing')"
	args = {"v": _variants(num), "limit": limit + 1}
	if before:
		conditions = f"({conditions}) AND creation < %(before)s"
		args["before"] = get_datetime(before)
	rows = frappe.db.sql(
		f"""SELECT {", ".join(f"`{f}`" for f in _FIELDS)} FROM `tabWhatsApp Message`
		WHERE {conditions} ORDER BY creation DESC LIMIT %(limit)s""",
		args,
		as_dict=True,
	)
	has_more = len(rows) > limit
	rows = list(reversed(rows[:limit]))

	read = None
	if cint(mark_read) and not before:
		read = _mark_read(num)
		if read:
			for r in rows:
				if r.type == "Incoming" and not r.butler_read_at:
					r.butler_read_by, r.butler_read_at = read["user"], read["at"]

	out = {"messages": [_message_row(r) for r in rows], "has_more": has_more}
	if not before:
		out["info"] = get_conversation_info(num)
		out["notes"] = _notes(out["info"].get("contact"))
	return out


@frappe.whitelist()
def get_message(name: str) -> dict:
	_require()
	m = frappe.db.get_value(MSG, name, _FIELDS + ["from", "to"], as_dict=True)
	if not m:
		frappe.throw(_("Message not found"), frappe.DoesNotExistError)
	access.check_access(m.get("from") if m.type == "Incoming" else m.to)
	return _message_row(m)


def _mark_read(num: str) -> dict | None:
	unread = frappe.get_all(
		MSG,
		filters={"type": "Incoming", "from": ["in", _variants(num)], "butler_read_at": ["is", "not set"]},
		fields=["name", "whatsapp_account"],
		order_by="creation desc",
	)
	if not unread:
		return None
	user, at = frappe.session.user, now_datetime()
	frappe.db.sql(
		"""UPDATE `tabWhatsApp Message` SET butler_read_by = %(u)s, butler_read_at = %(at)s
		WHERE name IN %(names)s AND butler_read_at IS NULL""",
		{"u": user, "at": at, "names": [u.name for u in unread]},
	)
	contact = _contact(num)
	if contact:
		frappe.db.set_value(CONTACT, contact, "is_read", 1, update_modified=False)
	# Blue ticks for the customer, only if enabled on the WhatsApp Account
	newest = unread[0]
	if newest.whatsapp_account and frappe.db.get_value(
		"WhatsApp Account", newest.whatsapp_account, "allow_auto_read_receipt"
	):
		frappe.enqueue(
			"butler_whatsapp.inbox.send_read_receipt", queue="short", enqueue_after_commit=True,
			message=newest.name,
		)
	publish(num, "read")
	return {"user": user, "at": at}


def send_read_receipt(message: str):
	try:
		frappe.get_doc(MSG, message).send_read_receipt()
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="WhatsApp read receipt failed", message=frappe.get_traceback())


@frappe.whitelist()
def get_conversation_info(number: str) -> dict:
	num = _require_number(number)
	matches = match_parties(num)
	frappe.cache.set_value(f"butler_wa_match:{num}", matches, expires_in_sec=MATCH_CACHE_TTL)
	profile = frappe.db.get_value(
		MSG, {"type": "Incoming", "from": ["in", _variants(num)], "profile_name": ["is", "set"]},
		"profile_name", order_by="creation desc",
	)
	# a registered number gets a WhatsApp Contact so the match can be stored
	contact_name = _contact(num, create=bool(matches), display=profile)
	contact = frappe.db.get_value(
		CONTACT, contact_name,
		["name", "contact_name", "butler_customer", "butler_lead", "butler_no_auto_link", "butler_assigned_to",
		 "butler_status"],
		as_dict=True,
	) if contact_name else None

	auto = {}
	if contact and not contact.butler_customer and not contact.butler_lead and matches \
		and not contact.butler_no_auto_link:
		auto = _auto_link(contact.name, matches)
		contact.update(auto)

	last_in = _last_incoming(num)
	party = _party(contact)
	return {
		"number": num,
		"contact": contact.name if contact else None,
		"contact_name": contact.contact_name if contact else None,
		"profile_name": profile,
		**party,
		"auto_linked": bool(auto),
		"matches": matches,
		"in_window": within_service_window(last_in, now_datetime()),
		"last_incoming": str(last_in) if last_in else None,
		"can_create_lead": bool(frappe.has_permission("Lead", "create")),
		"can_create_customer": bool(frappe.has_permission("Customer", "create")),
		"can_send": bool(frappe.has_permission(MSG, "create")),
		"assigned_to": contact.butler_assigned_to if contact else None,
		"assigned_name": _user_name(contact.butler_assigned_to) if contact else None,
		"status": (contact.butler_status if contact else None) or "Open",
		"supervisor": access.is_supervisor(),
	}


# ------------------------------------------------------------------ sending


def _log(account: str, num: str, message_id: str, content_type: str, message: str = "",
	attach: str | None = None, message_type: str = "Manual", template: str | None = None,
	reply_to: str | None = None) -> str:
	"""Store an already-sent message without frappe_whatsapp sending it again."""
	msg = frappe.new_doc(MSG)
	msg.update(
		{
			"type": "Outgoing",
			"to": num,
			"message": message,
			"content_type": content_type,
			"attach": attach,
			"message_type": message_type,
			"template": template,
			"message_id": message_id,
			"status": "Success",
			"whatsapp_account": account,
			"is_reply": 1 if reply_to else 0,
			"reply_to_message_id": reply_to,
		}
	)
	now = now_datetime()
	msg.owner = msg.modified_by = frappe.session.user
	msg.creation = msg.modified = now
	msg.set_new_name()
	msg.db_insert()
	contact = _contact(num, create=True)
	frappe.db.set_value(CONTACT, contact, "last_message", message or attach or "", update_modified=False)
	claim_if_unassigned(num)
	publish(num, "outgoing", msg.name)
	return msg.name


def _check_window(num: str):
	if not within_service_window(_last_incoming(num), now_datetime()):
		frappe.throw(
			_("The customer has not messaged you in the last 24 hours. WhatsApp only allows approved "
			  "templates outside this window."),
			title=_("Outside the 24-hour window"),
		)


@frappe.whitelist(methods=["POST"])
def send_text(number: str, message: str, reply_to: str | None = None) -> dict:
	num = _require_number(number, "create")
	text = (message or "").strip()
	if not text:
		frappe.throw(_("Message is empty"))
	if len(text) > 4096:
		frappe.throw(_("Message is too long (max 4096 characters)"))
	_check_window(num)
	account = _account()
	try:
		message_id = _client(account).send(with_reply(text_message(num, text), reply_to))
	except WhatsAppError as e:
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))
	name = _log(account.name, num, message_id, "text", text, reply_to=reply_to)
	return get_message(name)


@frappe.whitelist(methods=["POST"])
def send_file(number: str, file_url: str, caption: str | None = None, reply_to: str | None = None) -> dict:
	"""Send a file uploaded by the user (File record) to the customer."""
	num = _require_number(number, "create")
	file_doc = _own_file(file_url)
	_check_window(num)
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	mime = mimetypes.guess_type(file_doc.file_name or file_url)[0] or "application/octet-stream"
	return _send_media(num, file_doc, content, mime, caption, reply_to)


@frappe.whitelist(methods=["POST"])
def send_voice(number: str, file_url: str, reply_to: str | None = None) -> dict:
	"""Voice note recorded in the browser. Chrome records WebM, which WhatsApp
	does not accept, so it is remuxed to Ogg/Opus first (voice.py)."""
	from butler_whatsapp.voice import VoiceError, webm_to_ogg

	num = _require_number(number, "create")
	file_doc = _own_file(file_url)
	_check_window(num)
	content = file_doc.get_content()
	name = (file_doc.file_name or "").lower()
	if name.endswith(".webm"):
		try:
			content = webm_to_ogg(content)
		except VoiceError as e:
			frappe.throw(_("Could not convert the recording: {0}").format(str(e)))
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"voice-{frappe.generate_hash(length=8)}.ogg",
				"content": content,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		mime = "audio/ogg"
	elif name.endswith((".m4a", ".mp4")):
		mime = "audio/mp4"
	elif name.endswith(".ogg"):
		mime = "audio/ogg"
	else:
		mime = mimetypes.guess_type(name)[0] or "audio/mpeg"
	return _send_media(num, file_doc, content, mime, None, reply_to)


def _own_file(file_url: str):
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		frappe.throw(_("File not found"))
	file_doc = frappe.get_doc("File", file_name)
	if file_doc.owner != frappe.session.user:
		file_doc.check_permission("read")
	return file_doc


def _send_media(num, file_doc, content, mime, caption=None, reply_to=None) -> dict:
	account = _account()
	api = _client(account)
	caption = (caption or "").strip() or None
	try:
		kind = media_kind(mime, len(content))
		media_id = api.upload_media(content, file_doc.file_name, mime)
		message_id = api.send(with_reply(media_message(num, kind, media_id, file_doc.file_name, caption), reply_to))
	except WhatsAppError as e:
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))

	name = _log(account.name, num, message_id, kind, caption or "", file_doc.file_url, reply_to=reply_to)
	if not file_doc.attached_to_doctype:
		frappe.db.set_value(
			"File", file_doc.name,
			{"attached_to_doctype": MSG, "attached_to_name": name, "attached_to_field": "attach"},
			update_modified=False,
		)
	return get_message(name)


# --------------------------------------------------------- lead / customer


def _contact_for_link(num: str) -> str:
	profile = frappe.db.get_value(
		MSG, {"type": "Incoming", "from": ["in", _variants(num)], "profile_name": ["is", "set"]},
		"profile_name", order_by="creation desc",
	)
	return _contact(num, create=True, display=profile)


def _ensure_lead_source():
	if frappe.db.exists("DocType", "UTM Source") and not frappe.db.exists("UTM Source", LEAD_SOURCE):
		frappe.get_doc({"doctype": "UTM Source", "name": LEAD_SOURCE, "__newname": LEAD_SOURCE}).insert(
			ignore_permissions=True
		)


@frappe.whitelist(methods=["POST"])
def create_lead(number: str, lead_name: str, company_name: str | None = None, notes: str | None = None) -> dict:
	num = _require_number(number, "write")
	frappe.has_permission("Lead", "create", throw=True)
	_ensure_lead_source()
	lead = frappe.new_doc("Lead")
	lead.update(
		{
			"first_name": (lead_name or "").strip() or ("+" + num),
			"company_name": (company_name or "").strip() or None,
			"mobile_no": "+" + num,
			"whatsapp_no": "+" + num,
		}
	)
	if lead.meta.has_field("utm_source") and frappe.db.exists("UTM Source", LEAD_SOURCE):
		lead.utm_source = LEAD_SOURCE
	elif lead.meta.has_field("source") and frappe.db.exists("Lead Source", LEAD_SOURCE):
		lead.source = LEAD_SOURCE
	lead.insert()
	if notes:
		lead.add_comment("Comment", frappe.utils.escape_html(notes))
	contact = _contact_for_link(num)
	frappe.db.set_value(CONTACT, contact, "butler_lead", lead.name, update_modified=False)
	frappe.cache.delete_value(f"butler_wa_match:{num}")
	publish(num, "party")
	return {"doctype": "Lead", "name": lead.name}


@frappe.whitelist(methods=["POST"])
def create_customer(number: str, customer_name: str, customer_type: str = "Individual") -> dict:
	num = _require_number(number, "write")
	frappe.has_permission("Customer", "create", throw=True)
	contact = _contact_for_link(num)
	lead = frappe.db.get_value(CONTACT, contact, "butler_lead")
	if lead and frappe.db.exists("Lead", lead):
		from erpnext.crm.doctype.lead.lead import make_customer

		customer = make_customer(lead)
	else:
		customer = frappe.new_doc("Customer")
		customer.customer_group = frappe.db.get_single_value("Selling Settings", "customer_group") or \
			frappe.db.get_default("Customer Group")
		customer.territory = frappe.db.get_single_value("Selling Settings", "territory") or \
			frappe.db.get_default("Territory")
		customer.mobile_no = "+" + num  # ERPNext creates the primary Contact from it
	if customer_name and customer_name.strip():
		customer.customer_name = customer_name.strip()
	if customer_type in ("Individual", "Company", "Partnership"):
		customer.customer_type = customer_type
	customer.insert()
	frappe.db.set_value(CONTACT, contact, "butler_customer", customer.name, update_modified=False)
	frappe.cache.delete_value(f"butler_wa_match:{num}")
	publish(num, "party")
	return {"doctype": "Customer", "name": customer.name}


@frappe.whitelist(methods=["POST"])
def link_party(number: str, doctype: str, name: str) -> dict:
	_require("write")
	if doctype not in ("Customer", "Lead"):
		frappe.throw(_("Only Customer or Lead can be linked"))
	frappe.has_permission(doctype, "read", name, throw=True)
	num = _require_number(number, "write")
	contact = _contact_for_link(num)
	field = "butler_customer" if doctype == "Customer" else "butler_lead"
	frappe.db.set_value(CONTACT, contact, field, name, update_modified=False)
	publish(num, "party")
	return get_conversation_info(num)


@frappe.whitelist(methods=["POST"])
def unlink_party(number: str, doctype: str) -> dict:
	_require("write")
	if doctype not in ("Customer", "Lead"):
		frappe.throw(_("Only Customer or Lead can be unlinked"))
	num = _require_number(number, "write")
	contact = _contact(num)
	if contact:
		field = "butler_customer" if doctype == "Customer" else "butler_lead"
		# the user chose: never auto-link this conversation again
		frappe.db.set_value(CONTACT, contact, {field: None, "butler_no_auto_link": 1}, update_modified=False)
	publish(num, "party")
	return get_conversation_info(num)


# ------------------------------------------------------- incoming files


def make_attachment_private(doc, method=None):
	"""WhatsApp Message on_update: incoming media is saved by frappe_whatsapp as a
	public file; move it to private files (only users who can read the message
	can then open it)."""
	if doc.type != "Incoming" or not (doc.attach or "").startswith("/files/"):
		return
	try:
		name = frappe.db.get_value(
			"File", {"file_url": doc.attach, "attached_to_doctype": MSG, "attached_to_name": doc.name}, "name"
		)
		if not name:
			return
		file_doc = frappe.get_doc("File", name)
		file_doc.is_private = 1
		file_doc.attached_to_field = "attach"
		file_doc.save(ignore_permissions=True)  # moves the file and updates doc.attach in the DB
		doc.attach = file_doc.file_url
	except Exception:
		frappe.log_error(title="WhatsApp: could not make file private", message=frappe.get_traceback())


def make_existing_attachments_private():
	for row in frappe.get_all(
		MSG, filters={"type": "Incoming", "attach": ["like", "/files/%"]}, fields=["name", "type", "attach"]
	):
		make_attachment_private(frappe._dict(row))


def on_message_update(doc, method=None):
	"""WhatsApp Message on_update: private media + live status/media updates."""
	make_attachment_private(doc)
	num = normalize_msisdn(doc.get("from") if doc.type == "Incoming" else doc.to)
	if not num:
		return
	if doc.has_value_changed("status") or doc.has_value_changed("attach"):
		publish(num, "status" if doc.type == "Outgoing" else "media", doc.name)


@frappe.whitelist(methods=["POST"])
def mark_read(number: str) -> bool:
	"""Called by the inbox when a new message arrives in the open conversation."""
	return bool(_mark_read(_require_number(number)))


# ------------------------------------------------- assignment / status / notes


def _set_contact(num: str, values: dict) -> str:
	contact = _contact_for_link(num)
	frappe.db.set_value(CONTACT, contact, values, update_modified=True)
	return contact


def claim_if_unassigned(num: str, user: str | None = None):
	"""A non-supervisor who writes to a customer becomes responsible for that
	conversation (otherwise they could not see it afterwards)."""
	user = user or frappe.session.user
	if user in ("Administrator", "Guest") or access.is_supervisor(user) or not access.restricted():
		return
	if not access.assignee(num):
		_set_contact(num, {"butler_assigned_to": user})


def _notify_assignment(num: str, user: str, by: str):
	title = _conversation_title(num)
	subject = f"أُسندت إليك محادثة واتساب: {title}"
	try:
		from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

		enqueue_create_notification(
			user,
			{
				"type": "Alert",
				"document_type": CONTACT,
				"document_name": _contact(num),
				"subject": subject,
				"from_user": by,
			},
		)
	except Exception:
		frappe.log_error(title="WhatsApp assignment notification failed", message=frappe.get_traceback())
	frappe.enqueue(
		"butler_whatsapp.push.notify_users_job",
		queue="short",
		enqueue_after_commit=True,
		users=[user],
		data={"title": "واتساب", "body": subject, "tag": f"butler-wa-{num}",
			"url": f"/desk/whatsapp-inbox?number={num}"},
	)
	frappe.publish_realtime("butler_wa_assigned", {"number": num, "title": title, "by": _user_name(by)},
		user=user, after_commit=True)


def _conversation_title(num: str) -> str:
	contact = _contact(num)
	c = frappe.db.get_value(CONTACT, contact, ["contact_name", "butler_customer", "butler_lead"], as_dict=True) \
		if contact else None
	if c and c.butler_customer:
		return frappe.db.get_value("Customer", c.butler_customer, "customer_name") or c.butler_customer
	if c and c.butler_lead:
		return frappe.db.get_value("Lead", c.butler_lead, "lead_name") or c.butler_lead
	if c and c.contact_name and c.contact_name.lstrip("+") != num:
		return c.contact_name
	return "+" + num


@frappe.whitelist(methods=["POST"])
def assign(number: str, user: str | None = None) -> dict:
	"""Assign (or with empty user: unassign) a conversation. Supervisors only."""
	_require("write")
	num = _num(number)
	if not access.is_supervisor():
		frappe.throw(_("Only supervisors can assign conversations"), frappe.PermissionError)
	if user and not frappe.db.exists("User", {"name": user, "enabled": 1}):
		frappe.throw(_("User {0} not found").format(user))
	previous = access.assignee(num)
	_set_contact(num, {"butler_assigned_to": user or None})
	_contact_comment(num, f"إسناد المحادثة إلى {_user_name(user)}" if user else "إلغاء إسناد المحادثة")
	if user and user != frappe.session.user:
		_notify_assignment(num, user, frappe.session.user)
	publish(num, "assign", extra_users=[u for u in (previous, user) if u])
	return get_conversation_info(num)


@frappe.whitelist(methods=["POST"])
def set_status(number: str, status: str) -> dict:
	if status not in ("Open", "Closed"):
		frappe.throw(_("Invalid status"))
	num = _require_number(number, "write")
	_set_contact(num, {"butler_status": status})
	_contact_comment(num, "إغلاق المحادثة" if status == "Closed" else "إعادة فتح المحادثة")
	publish(num, "status_change")
	return get_conversation_info(num)


def _contact_comment(num: str, text: str, comment_type: str = "Info"):
	contact = _contact_for_link(num)
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": comment_type,
			"reference_doctype": CONTACT,
			"reference_name": contact,
			"content": text,
			"comment_email": frappe.session.user,
			"comment_by": _user_name(frappe.session.user),
		}
	).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def add_note(number: str, note: str) -> dict:
	"""Internal note on a conversation (never sent to the customer)."""
	num = _require_number(number, "write")
	text = (note or "").strip()
	if not text:
		frappe.throw(_("Note is empty"))
	_contact_comment(num, frappe.utils.escape_html(text), "Comment")
	publish(num, "note")
	return {"notes": _notes(_contact(num))}


def _notes(contact: str | None) -> list[dict]:
	if not contact:
		return []
	rows = frappe.get_all(
		"Comment",
		filters={"reference_doctype": CONTACT, "reference_name": contact, "comment_type": ["in", ["Comment", "Info"]]},
		fields=["name", "comment_type", "content", "comment_email", "owner", "creation"],
		order_by="creation asc",
		limit=200,
	)
	return [
		{
			"name": r.name,
			"kind": "note" if r.comment_type == "Comment" else "event",
			"content": strip_html(r.content or ""),
			"by": _user_name(r.comment_email or r.owner),
			"creation": str(r.creation),
		}
		for r in rows
	]


def _next_pool_agent() -> str | None:
	s = access.settings()
	pool = [r.user for r in s.get("assign_pool") or [] if r.user and frappe.db.get_value("User", r.user, "enabled")]
	if not pool:
		return None
	last = s.get("last_assigned")
	nxt = pool[(pool.index(last) + 1) % len(pool)] if last in pool else pool[0]
	frappe.db.set_single_value(access.SETTINGS, "last_assigned", nxt)
	frappe.clear_document_cache(access.SETTINGS, access.SETTINGS)
	return nxt


def on_incoming(doc):
	"""Called from realtime.on_message_insert for every incoming message
	(same transaction, after whatsapp_chat created the WhatsApp Contact)."""
	num = normalize_msisdn(doc.get("from"))
	if not num:
		return
	contact = _contact(num)
	if not contact:
		return
	row = frappe.db.get_value(CONTACT, contact, ["butler_assigned_to", "butler_status"], as_dict=True)
	s = access.settings()
	values = {}
	reopen = s.get("reopen_on_incoming")
	if row.butler_status == "Closed" and (reopen is None or cint(reopen)):
		values["butler_status"] = "Open"
	if not row.butler_assigned_to and s.get("auto_assign"):
		user = _next_pool_agent()
		if user:
			values["butler_assigned_to"] = user
			_notify_assignment(num, user, "Administrator")
	if values:
		frappe.db.set_value(CONTACT, contact, values, update_modified=False)
	_enrich_message(doc)


def _enrich_message(doc):
	"""frappe_whatsapp stores no text for location / contacts messages; rebuild it
	from the raw webhook payload it logged just before."""
	from butler_whatsapp.meta import describe_contacts, describe_location

	if doc.get("content_type") not in ("location", "contacts") or (doc.get("message") or "").strip():
		return
	try:
		raw = frappe.db.sql(
			"""SELECT meta_data FROM `tabWhatsApp Notification Log`
			WHERE template = 'Webhook' AND meta_data LIKE %s ORDER BY creation DESC LIMIT 1""",
			(f"%{doc.message_id}%",),
		)
		if not raw:
			return
		payload = frappe.parse_json(raw[0][0])
		for change in (payload.get("entry") or [{}])[0].get("changes") or []:
			for m in (change.get("value") or {}).get("messages") or []:
				if m.get("id") != doc.message_id:
					continue
				text = describe_location(m.get("location") or {}) if doc.content_type == "location" \
					else describe_contacts(m.get("contacts") or [])
				if text:
					doc.db_set("message", text, update_modified=False)
				return
	except Exception:
		frappe.log_error(title="WhatsApp: could not read location/contact message", message=frappe.get_traceback())


# --------------------------------------------------------- context / search


@frappe.whitelist()
def get_inbox_context() -> dict:
	_require()
	replies = frappe.get_all(
		"WhatsApp Quick Reply", fields=["name", "title", "shortcut", "message"], order_by="title asc", limit=500
	) if frappe.db.exists("DocType", "WhatsApp Quick Reply") else []
	return {
		"user": frappe.session.user,
		"supervisor": access.is_supervisor(),
		"restricted": access.restricted(),
		"agents": access.agents(),
		"quick_replies": replies,
		"can_settings": bool(frappe.has_permission(access.SETTINGS, "write")),
	}


@frappe.whitelist()
def search_targets(q: str) -> list[dict]:
	"""Customers, leads and contacts (with a mobile) for "new conversation"."""
	_require()
	q = (q or "").strip()
	if len(q) < 2:
		return []
	like = f"%{q}%"
	out = []
	for r in frappe.get_list(
		"Customer", filters={"disabled": 0, "mobile_no": ["is", "set"]},
		or_filters=[["customer_name", "like", like], ["mobile_no", "like", like], ["name", "like", like]],
		fields=["name", "customer_name", "mobile_no"], limit=10,
	):
		out.append({"doctype": "Customer", "name": r.name, "title": r.customer_name, "phone": r.mobile_no})
	for r in frappe.get_list(
		"Lead", or_filters=[["lead_name", "like", like], ["company_name", "like", like], ["mobile_no", "like", like]],
		filters={"mobile_no": ["is", "set"]}, fields=["name", "lead_name", "company_name", "mobile_no"], limit=10,
	):
		out.append({"doctype": "Lead", "name": r.name, "title": r.lead_name or r.company_name, "phone": r.mobile_no})
	for r in frappe.get_list(
		"Contact", or_filters=[["full_name", "like", like], ["mobile_no", "like", like]],
		filters={"mobile_no": ["is", "set"]}, fields=["name", "full_name", "mobile_no"], limit=10,
	):
		out.append({"doctype": "Contact", "name": r.name, "title": r.full_name, "phone": r.mobile_no})
	for row in out:
		row["number"] = normalize_msisdn(row["phone"])
	return [r for r in out if r["number"]]


@frappe.whitelist(methods=["POST"])
def start_conversation(number: str, doctype: str | None = None, name: str | None = None) -> dict:
	"""Open (or create) the conversation for a number, optionally linking it to
	the Customer / Lead it was started from."""
	_require("create")
	num = _num(number)
	who = access.assignee(num)
	if who and who != frappe.session.user and not access.is_supervisor():
		frappe.throw(_("This conversation is assigned to {0}").format(_user_name(who)), frappe.PermissionError)
	contact = _contact_for_link(num)
	if doctype in ("Customer", "Lead") and name:
		frappe.has_permission(doctype, "read", name, throw=True)
		field = "butler_customer" if doctype == "Customer" else "butler_lead"
		if not frappe.db.get_value(CONTACT, contact, field):
			frappe.db.set_value(CONTACT, contact, field, name, update_modified=False)
	claim_if_unassigned(num)
	return {"number": num}


# ----------------------------------------------------------------- templates


def _template_rows(account_name: str) -> list[dict]:
	rows = [
		r for r in frappe.get_all(
			"WhatsApp Templates",
			filters={"status": "APPROVED"},
			fields=["name", "actual_name", "template_name", "language_code", "template", "header_type", "header",
				"footer", "sample", "category", "whatsapp_account"],
			order_by="actual_name asc",
		)
		if not r.whatsapp_account or r.whatsapp_account == account_name
	]
	for r in rows:
		r["body_vars"] = count_body_variables(r.get("template"))
		r["header_vars"] = count_body_variables(r.get("header")) if r.get("header_type") == "TEXT" else 0
	return rows


@frappe.whitelist()
def get_templates(number: str | None = None) -> dict:
	_require()
	account = _account()
	defaults = {}
	if number:
		num = _require_number(number)
		defaults["name"] = _conversation_title(num)
	return {"templates": _template_rows(account.name), "defaults": defaults}


@frappe.whitelist(methods=["POST"])
def send_template(
	number: str,
	template: str,
	body_params: str | list | None = None,
	header_params: str | list | None = None,
	header_file_url: str | None = None,
) -> dict:
	"""Send an approved template (works outside the 24-hour window)."""
	_require("create")
	num = _num(number)
	who = access.assignee(num)
	if who and who != frappe.session.user and not access.is_supervisor():
		frappe.throw(_("This conversation is assigned to {0}").format(_user_name(who)), frappe.PermissionError)
	tpl = frappe.get_doc("WhatsApp Templates", template)
	if tpl.status != "APPROVED":
		frappe.throw(_("Template {0} is not approved").format(tpl.actual_name or tpl.name))
	body = frappe.parse_json(body_params) if isinstance(body_params, str) else (body_params or [])
	head = frappe.parse_json(header_params) if isinstance(header_params, str) else (header_params or [])
	if len(body) != count_body_variables(tpl.template):
		frappe.throw(_("Template needs {0} value(s)").format(count_body_variables(tpl.template)))

	account = _account()
	api = _client(account)
	header = None
	attach = None
	try:
		if tpl.header_type == "TEXT" and count_body_variables(tpl.header):
			header = {"type": "text", "text": str((head or [""])[0])}
		elif tpl.header_type in ("DOCUMENT", "IMAGE"):
			url = header_file_url or tpl.sample
			if not url:
				frappe.throw(_("This template needs a file in its header"))
			if header_file_url:
				file_doc = _own_file(url)
			else:
				sample = frappe.db.get_value("File", {"file_url": url}, "name")
				if not sample:
					frappe.throw(_("Choose a file for the template header"))
				file_doc = frappe.get_doc("File", sample)
			content = file_doc.get_content()
			if isinstance(content, str):
				content = content.encode()
			mime = mimetypes.guess_type(file_doc.file_name)[0] or (
				"application/pdf" if tpl.header_type == "DOCUMENT" else "image/jpeg"
			)
			media_id = api.upload_media(content, file_doc.file_name, mime)
			kind = tpl.header_type.lower()
			header = {"type": kind, kind: {"id": media_id, **({"filename": file_doc.file_name} if kind == "document" else {})}}
			attach = file_doc.file_url
		message_id = api.send(template_message(num, tpl.actual_name or tpl.template_name, tpl.language_code, body, header))
	except WhatsAppError as e:
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))

	text = render_template(tpl.template, body)
	ctype = "text" if not attach else ("document" if tpl.header_type == "DOCUMENT" else "image")
	name = _log(account.name, num, message_id, ctype, text, attach, message_type="Template", template=tpl.name)
	return get_message(name)


# ------------------------------------------------------ ERPNext documents


_DOC_TYPES = ("Quotation", "Sales Order", "Sales Invoice")


@frappe.whitelist()
def get_party_documents(number: str) -> list[dict]:
	"""Recent quotations / orders / invoices of the linked customer or lead."""
	num = _require_number(number)
	contact = _contact(num)
	c = frappe.db.get_value(CONTACT, contact, ["butler_customer", "butler_lead"], as_dict=True) if contact else None
	if not c or not (c.butler_customer or c.butler_lead):
		return []
	out = []
	for dt in _DOC_TYPES:
		if not frappe.has_permission(dt, "read"):
			continue
		if dt == "Quotation":
			parties = [("Customer", c.butler_customer), ("Lead", c.butler_lead)]
			for party_type, party in parties:
				if not party:
					continue
				for r in frappe.get_list(dt, filters={"quotation_to": party_type, "party_name": party, "docstatus": ["<", 2]},
					fields=["name", "transaction_date as date", "grand_total", "currency", "status"],
					order_by="creation desc", limit=15):
					out.append({"doctype": dt, **r})
		elif c.butler_customer:
			date_field = "posting_date" if dt == "Sales Invoice" else "transaction_date"
			for r in frappe.get_list(dt, filters={"customer": c.butler_customer, "docstatus": ["<", 2]},
				fields=["name", f"{date_field} as date", "grand_total", "currency", "status"],
				order_by="creation desc", limit=15):
				out.append({"doctype": dt, **r})
	out.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
	for r in out:
		r["date"] = str(r.get("date") or "")
	return out[:40]
