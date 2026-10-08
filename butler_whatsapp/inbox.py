"""Butler WhatsApp inbox (Desk page "whatsapp-inbox").

- Conversations are grouped by the customer's number straight from
  "WhatsApp Message", so every message shows up (templates and PDFs too).
- Read tracking: the first user who opens a conversation is stored on each
  unread incoming message (butler_read_by / butler_read_at).
- Files are sent by uploading to Meta and sending the media id, so private
  files never need a public URL.
- A conversation can be linked to a Lead or Customer. Existing parties are
  found automatically by phone number (last 9 digits).
"""

from __future__ import annotations

import mimetypes

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_fullname, now_datetime, strip_html

from butler_whatsapp.api import _account, _client, _last_incoming
from butler_whatsapp.meta import (
	WhatsAppError,
	media_kind,
	media_message,
	normalize_msisdn,
	phone_tail,
	text_message,
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


def publish(num: str, kind: str, name: str | None = None):
	"""Tell every inbox (other agents too) that conversation `num` changed."""
	from butler_whatsapp.realtime import _agents

	data = {"number": num, "kind": kind, "name": name}
	for user in _agents():
		frappe.publish_realtime(UPDATE_EVENT, data, user=user, after_commit=True)


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
def get_conversations(search: str | None = None, limit: int = 60, offset: int = 0) -> list[dict]:
	_require()
	limit = min(cint(limit) or 60, 200)
	offset = cint(offset)
	cond, args = "", {"limit": limit, "offset": offset}
	if search:
		args["q"] = f"%{search.strip()}%"
		digits = "".join(ch for ch in search if ch.isdigit()).lstrip("0")
		parts = ["c.contact_name LIKE %(q)s", "cu.customer_name LIKE %(q)s", "l.lead_name LIKE %(q)s",
			"t.profile_name LIKE %(q)s"]
		if len(digits) >= 3:
			args["d"] = f"%{digits[-9:]}%"
			parts.append("t.num LIKE %(d)s")
		cond = "WHERE " + " OR ".join(parts)

	rows = frappe.db.sql(
		f"""
		SELECT t.num, t.last_at, t.unread, t.profile_name,
			c.name AS contact, c.contact_name, c.butler_customer, c.butler_lead, c.butler_no_auto_link,
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
	return cint(
		frappe.db.sql(
			"SELECT COUNT(*) FROM `tabWhatsApp Message` WHERE type = 'Incoming' AND butler_read_at IS NULL"
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
	_require()
	num = _num(number)
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
	return out


@frappe.whitelist()
def get_message(name: str) -> dict:
	_require()
	m = frappe.db.get_value(MSG, name, _FIELDS, as_dict=True)
	if not m:
		frappe.throw(_("Message not found"), frappe.DoesNotExistError)
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
	_require()
	num = _num(number)
	matches = match_parties(num)
	frappe.cache.set_value(f"butler_wa_match:{num}", matches, expires_in_sec=MATCH_CACHE_TTL)
	profile = frappe.db.get_value(
		MSG, {"type": "Incoming", "from": ["in", _variants(num)], "profile_name": ["is", "set"]},
		"profile_name", order_by="creation desc",
	)
	# a registered number gets a WhatsApp Contact so the match can be stored
	contact_name = _contact(num, create=bool(matches), display=profile)
	contact = frappe.db.get_value(
		CONTACT, contact_name, ["name", "contact_name", "butler_customer", "butler_lead", "butler_no_auto_link"],
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
	}


# ------------------------------------------------------------------ sending


def _log(account: str, num: str, message_id: str, content_type: str, message: str = "",
	attach: str | None = None) -> str:
	"""Store an already-sent message without frappe_whatsapp sending it again."""
	msg = frappe.new_doc(MSG)
	msg.update(
		{
			"type": "Outgoing",
			"to": num,
			"message": message,
			"content_type": content_type,
			"attach": attach,
			"message_type": "Manual",
			"message_id": message_id,
			"status": "Success",
			"whatsapp_account": account,
		}
	)
	now = now_datetime()
	msg.owner = msg.modified_by = frappe.session.user
	msg.creation = msg.modified = now
	msg.set_new_name()
	msg.db_insert()
	contact = _contact(num, create=True)
	frappe.db.set_value(CONTACT, contact, "last_message", message or attach or "", update_modified=False)
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
def send_text(number: str, message: str) -> dict:
	_require("create")
	num = _num(number)
	text = (message or "").strip()
	if not text:
		frappe.throw(_("Message is empty"))
	if len(text) > 4096:
		frappe.throw(_("Message is too long (max 4096 characters)"))
	_check_window(num)
	account = _account()
	try:
		message_id = _client(account).send(text_message(num, text))
	except WhatsAppError as e:
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))
	name = _log(account.name, num, message_id, "text", text)
	return get_message(name)


@frappe.whitelist(methods=["POST"])
def send_file(number: str, file_url: str, caption: str | None = None) -> dict:
	"""Send a file uploaded by the user (File record) to the customer."""
	_require("create")
	num = _num(number)
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		frappe.throw(_("File not found"))
	file_doc = frappe.get_doc("File", file_name)
	if file_doc.owner != frappe.session.user:
		file_doc.check_permission("read")
	_check_window(num)

	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	mime = mimetypes.guess_type(file_doc.file_name or file_url)[0] or "application/octet-stream"
	account = _account()
	api = _client(account)
	try:
		kind = media_kind(mime, len(content))
		media_id = api.upload_media(content, file_doc.file_name, mime)
		message_id = api.send(media_message(num, kind, media_id, file_doc.file_name, (caption or "").strip() or None))
	except WhatsAppError as e:
		frappe.throw(_("WhatsApp error: {0}").format(str(e)))

	name = _log(account.name, num, message_id, kind, (caption or "").strip(), file_doc.file_url)
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
	_require("write")
	num = _num(number)
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
	_require("write")
	num = _num(number)
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
	num = _num(number)
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
	num = _num(number)
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
	_require()
	return bool(_mark_read(_num(number)))
