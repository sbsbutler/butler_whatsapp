"""WhatsApp campaigns: one approved marketing template to many numbers.

Meta rules enforced here (they cannot be switched off from the screen):
- Only approved, switched-on MARKETING templates.
- The template text must tell the customer how to stop the messages.
- Only numbers with a recorded consent (opt-in). Opted-out numbers, numbers on
  the "never send campaigns" list and numbers that got a campaign recently are
  excluded and shown with the reason.
- Consent is checked again right before each message (a customer may stop
  in the middle of a campaign).
- Messages go out slowly (per minute), stop for the day at the number's
  daily messaging limit and pause if the quality rating turns red.
"""

from __future__ import annotations

import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from butler_whatsapp import access, compliance
from butler_whatsapp.meta import count_body_variables, keyword_list, normalize_msisdn, normalize_keyword

CAMPAIGN = "WhatsApp Campaign"
RECIPIENT = "WhatsApp Campaign Recipient"
CONTACT = "WhatsApp Contact"
MSG = "WhatsApp Message"

DRAFT, SCHEDULED, SENDING, PAUSED, COMPLETED, CANCELLED = (
	"Draft", "Scheduled", "Sending", "Paused", "Completed", "Cancelled")
PENDING, EXCLUDED, REMOVED, SENT, DELIVERED, READ, FAILED = (
	"Pending", "Excluded", "Removed", "Sent", "Delivered", "Read", "Failed")

R_NO_CONSENT = "No consent recorded"
R_OPTED_OUT = "Asked to stop messages"
R_DNC = "On the never-send list"
R_RECENT = "Got a campaign recently"
R_DUPLICATE = "Same number twice"
R_INVALID = "Invalid number"

# unique customers that can be reached with templates per 24 hours
TIER_LIMITS = {"TIER_50": 50, "TIER_250": 250, "TIER_1K": 1000, "TIER_2K": 2000, "TIER_10K": 10000,
	"TIER_100K": 100000, "TIER_UNLIMITED": None}

STATUS_FROM_META = {"sent": SENT, "delivered": DELIVERED, "read": READ, "failed": FAILED}
STATUS_RANK = {PENDING: 0, SENT: 1, DELIVERED: 2, READ: 3, FAILED: 4}


# -------------------------------------------------------------- pure checks


def has_stop_instructions(text: str | None, keywords: list[str]) -> bool:
	"""The template must tell the customer how to stop (e.g. "لإيقاف الرسائل أرسل: إيقاف")."""
	norm = normalize_keyword(text or "")
	return any(k and k in norm for k in (normalize_keyword(k) for k in keywords))


def body_for(values_text: str | None, name: str | None, fallback: str, count: int) -> list[str]:
	"""Template values for one customer; {name} becomes the customer's name."""
	lines = [l.strip() for l in (values_text or "").splitlines()]
	lines = [l for l in lines if l][:count]
	if len(lines) < count:
		frappe.throw(_("The template needs {0} value(s)").format(count))
	real = name if name and not re.fullmatch(r"\+?\d+", name.strip()) else ""
	return [l.replace("{name}", real or fallback) for l in lines]


def exclusion_reason(contact: dict, now, skip_days: int) -> str | None:
	if contact.get("butler_consent") == compliance.OPTED_OUT:
		return R_OPTED_OUT
	if cint(contact.get("butler_dnc")):
		return R_DNC
	if contact.get("butler_consent") != compliance.OPTED_IN:
		return R_NO_CONSENT
	last = contact.get("butler_last_campaign")
	if skip_days and last and get_datetime(last) > now - timedelta(days=skip_days):
		return R_RECENT
	return None


def daily_room(tier: str | None, sent_last_24h: int) -> int | None:
	"""How many more customers can get a template today (None = no limit known)."""
	limit = TIER_LIMITS.get(tier or "")
	if limit is None:
		return None
	return max(0, limit - sent_last_24h)


# ------------------------------------------------------------ validation


def _template(name: str):
	return frappe.get_doc("WhatsApp Templates", name)


def check_template(tpl) -> None:
	if tpl.status != "APPROVED":
		frappe.throw(_("Template {0} is not approved by Meta").format(tpl.actual_name or tpl.name))
	if not compliance.template_is_enabled(tpl):
		frappe.throw(_("Template {0} is switched off").format(tpl.actual_name or tpl.name))
	if (tpl.get("category") or "").upper() != "MARKETING":
		frappe.throw(_("Campaigns can only use MARKETING templates (greetings and offers are marketing for Meta)."))
	s = access.settings()
	words = keyword_list(s.get("opt_out_keywords")) or ["إيقاف", "STOP"]
	text = "\n".join(str(tpl.get(f) or "") for f in ("template", "footer"))
	if not has_stop_instructions(text, words):
		frappe.throw(_("The template must tell the customer how to stop the messages, e.g. in the footer: "
			"\"لإيقاف الرسائل أرسل: إيقاف\". Create a new version of the template with this line."))


def validate_campaign(doc):
	if doc.template:
		tpl = _template(doc.template)
		check_template(tpl)
		count = count_body_variables(tpl.template)
		if count and not (doc.body_values or "").strip():
			doc.body_values = "{name}" if count == 1 else ""
		if tpl.header_type in ("IMAGE", "DOCUMENT") and not (doc.header_file or tpl.sample):
			frappe.throw(_("This template needs an image / file in its header"))
	doc.per_minute = min(max(cint(doc.per_minute) or 20, 1), 60)
	if doc.skip_recent_days is None:
		doc.skip_recent_days = 7
	if doc.status in (SENDING, SCHEDULED) and doc.has_value_changed("template"):
		frappe.throw(_("Pause the campaign before changing its template"))


# ------------------------------------------------------------- recipients


def _audience_contacts(doc) -> list[dict]:
	fields = ["name", "contact_name", "mobile_no", "butler_customer", "butler_lead", "butler_consent",
		"butler_team"] + [f for f in ("butler_user", "butler_dnc", "butler_last_campaign", "butler_import")
		if frappe.db.has_column(CONTACT, f)]
	filters = {}
	if doc.team:
		filters["butler_team"] = doc.team
	if doc.number_import:
		filters["butler_import"] = doc.number_import
	rows = frappe.get_all(CONTACT, filters=filters, fields=fields, limit_page_length=0, order_by="creation asc")
	out = []
	for c in rows:
		kinds = []
		if c.butler_customer:
			kinds.append("customers")
		if c.butler_lead:
			kinds.append("leads")
		if c.get("butler_user"):
			kinds.append("staff")
		if not kinds:
			kinds.append("others")
		if any(cint(doc.get("to_" + k)) for k in kinds):
			out.append(c)
	return out


@frappe.whitelist(methods=["POST"])
def build_recipients(name: str) -> dict:
	"""Fill the recipients table: who will get the message and who is excluded (and why)."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(CAMPAIGN, name)
	if doc.status not in (DRAFT, PAUSED):
		frappe.throw(_("Pause the campaign first"))
	sent_before = {r.number: r for r in doc.recipients if r.status in (SENT, DELIVERED, READ, FAILED)}
	removed = {r.number for r in doc.recipients if r.status == REMOVED}
	now = now_datetime()
	seen = set()
	rows = []
	for c in _audience_contacts(doc):
		num = normalize_msisdn(c.mobile_no)
		row = {"contact": c.name, "number": num or c.mobile_no, "contact_name": c.contact_name}
		if num in sent_before:
			continue
		if not num:
			row.update(status=EXCLUDED, reason=_(R_INVALID))
		elif num in seen:
			row.update(status=EXCLUDED, reason=_(R_DUPLICATE))
		elif num in removed:
			row.update(status=REMOVED, reason=_("Removed from this campaign"))
		else:
			reason = exclusion_reason(c, now, cint(doc.skip_recent_days))
			row.update(status=EXCLUDED if reason else PENDING, reason=_(reason) if reason else "")
		seen.add(num)
		rows.append(row)
	doc.set("recipients", [r for r in doc.recipients if r.number in sent_before])
	for row in rows:
		doc.append("recipients", row)
	_recount(doc)
	doc.estimated_cost = estimate_cost(doc.eligible)
	doc.save()
	return summary(doc)


def summary(doc) -> dict:
	reasons = {}
	for r in doc.recipients:
		if r.status == EXCLUDED:
			reasons[r.reason] = reasons.get(r.reason, 0) + 1
	return {"total": doc.total, "eligible": doc.eligible, "excluded": doc.excluded, "reasons": reasons,
		"estimated_cost": doc.estimated_cost, "currency": access.settings().get("billing_currency")}


def _recount(doc):
	counts = {}
	for r in doc.recipients:
		counts[r.status] = counts.get(r.status, 0) + 1
	doc.total = len(doc.recipients)
	doc.eligible = counts.get(PENDING, 0) + sum(counts.get(s, 0) for s in (SENT, DELIVERED, READ, FAILED))
	doc.excluded = counts.get(EXCLUDED, 0) + counts.get(REMOVED, 0)
	doc.sent = sum(counts.get(s, 0) for s in (SENT, DELIVERED, READ))
	doc.delivered = counts.get(DELIVERED, 0) + counts.get(READ, 0)
	doc.read_count = counts.get(READ, 0)
	doc.failed = counts.get(FAILED, 0)


def estimate_cost(count: int) -> float:
	"""Average MARKETING cost per message over the last 60 days (from Meta's numbers)."""
	if not count or not frappe.db.table_exists("WhatsApp Cost"):
		return 0
	row = frappe.db.sql(
		"""SELECT SUM(cost), SUM(volume) FROM `tabWhatsApp Cost`
		WHERE pricing_category = 'MARKETING' AND pricing_type = 'REGULAR' AND date >= %s""",
		(frappe.utils.add_days(frappe.utils.today(), -60),),
	)
	cost, volume = (row[0] if row else (0, 0))
	if not volume:
		return 0
	return round(flt(cost) / flt(volume) * count, 2)


@frappe.whitelist(methods=["POST"])
def remove_recipients(name: str, rows: str | list, never_again: int = 0) -> dict:
	"""Remove chosen numbers from this campaign; never_again also adds them to the never-send list."""
	frappe.only_for("System Manager")
	rows = frappe.parse_json(rows) if isinstance(rows, str) else rows
	doc = frappe.get_doc(CAMPAIGN, name)
	for r in doc.recipients:
		if r.name in rows and r.status in (PENDING, EXCLUDED):
			r.status = REMOVED
			r.reason = _("On the never-send list") if cint(never_again) else _("Removed from this campaign")
			if cint(never_again) and r.contact:
				frappe.db.set_value(CONTACT, r.contact, "butler_dnc", 1)
	_recount(doc)
	doc.save()
	return summary(doc)


# --------------------------------------------------------------- start/stop


@frappe.whitelist(methods=["POST"])
def start(name: str) -> dict:
	frappe.only_for("System Manager")
	doc = frappe.get_doc(CAMPAIGN, name)
	if doc.status not in (DRAFT, PAUSED):
		frappe.throw(_("The campaign is {0}").format(_(doc.status)))
	check_template(_template(doc.template))
	if not any(r.status == PENDING for r in doc.recipients):
		frappe.throw(_("Nobody to send to. Press \"Find recipients\" first."))
	s = access.settings()
	if (s.get("quality_rating") or "") == "RED":
		frappe.throw(_("The quality rating of the WhatsApp number is red. Wait until it improves before sending a campaign."))
	doc.status = SCHEDULED if doc.send_at and get_datetime(doc.send_at) > now_datetime() else SENDING
	doc.last_error = ""
	doc.save()
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def pause(name: str) -> dict:
	frappe.only_for("System Manager")
	doc = frappe.get_doc(CAMPAIGN, name)
	if doc.status in (SENDING, SCHEDULED):
		doc.status = PAUSED
		doc.save()
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def cancel(name: str) -> dict:
	frappe.only_for("System Manager")
	doc = frappe.get_doc(CAMPAIGN, name)
	if doc.status in (COMPLETED, CANCELLED):
		return {"status": doc.status}
	doc.status = CANCELLED
	doc.save()
	return {"status": doc.status}


# ------------------------------------------------------------------ sending


def _sent_last_24h() -> int:
	"""Unique customers that got a template in the last 24 hours (business-initiated)."""
	since = now_datetime() - timedelta(hours=24)
	return cint(frappe.db.sql(
		"""SELECT COUNT(DISTINCT `to`) FROM `tabWhatsApp Message`
		WHERE type = 'Outgoing' AND message_type = 'Template' AND creation >= %s""", (since,))[0][0])


def process_campaigns():
	"""Scheduler (every minute): start due campaigns and send the next batch."""
	if not frappe.db.table_exists(CAMPAIGN):
		return
	now = now_datetime()
	for name in frappe.get_all(CAMPAIGN, filters={"status": SCHEDULED, "send_at": ["<=", now]}, pluck="name"):
		frappe.db.set_value(CAMPAIGN, name, "status", SENDING)
	frappe.db.commit()
	for name in frappe.get_all(CAMPAIGN, filters={"status": SENDING}, pluck="name", order_by="modified asc"):
		lock = f"butler_wa_campaign_lock:{name}"
		if frappe.cache.get_value(lock):
			continue
		frappe.cache.set_value(lock, 1, expires_in_sec=170)
		try:
			send_batch(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"WhatsApp campaign {name} failed", message=frappe.get_traceback())
		finally:
			frappe.cache.delete_value(lock)


def send_batch(name: str):
	from butler_whatsapp.inbox import send_template_core

	frappe.set_user("Administrator")
	doc = frappe.get_doc(CAMPAIGN, name)
	s = access.settings()
	if (s.get("quality_rating") or "") == "RED":
		_stop(doc, PAUSED, _("Paused automatically: the quality rating of the number turned red."))
		return
	pending = [r for r in doc.recipients if r.status == PENDING]
	if not pending:
		_stop(doc, COMPLETED, "")
		return
	room = daily_room(s.get("messaging_limit_tier"), _sent_last_24h())
	batch = pending[: cint(doc.per_minute) or 20]
	if room is not None:
		if room <= 0:
			frappe.db.set_value(CAMPAIGN, name, "last_error",
				_("The daily messaging limit of the number is reached; sending continues automatically later."))
			frappe.db.commit()
			return
		batch = batch[:room]
	tpl = _template(doc.template)
	check_template(tpl)
	count = count_body_variables(tpl.template)
	fallback = doc.name_fallback or _("Customer")
	for r in batch:
		# the campaign may have been paused meanwhile
		if frappe.db.get_value(CAMPAIGN, name, "status") != SENDING:
			return
		values = {"sent_at": now_datetime()}
		try:
			contact = frappe.db.get_value(CONTACT, r.contact, ["butler_consent", "butler_dnc"], as_dict=True) or {}
			if cint(contact.get("butler_dnc")):
				raise frappe.ValidationError(_(R_DNC))
			if contact.get("butler_consent") != compliance.OPTED_IN:
				# campaigns need a recorded consent, even if the customer wrote recently
				raise frappe.ValidationError(_(R_OPTED_OUT if contact.get("butler_consent") else R_NO_CONSENT))
			body = body_for(doc.body_values, r.contact_name, fallback, count)
			message = send_template_core(r.number, tpl, body, header_file_url=doc.header_file or None,
				reference=(CAMPAIGN, name))
			values.update(status=SENT, message=message, reason="")
			if r.contact:
				frappe.db.set_value(CONTACT, r.contact, "butler_last_campaign", now_datetime(), update_modified=False)
		except Exception as e:
			text = frappe.utils.strip_html(str(e))[:140]
			consent = compliance.get_consent(r.number)["state"]
			values.update(status=EXCLUDED if consent != compliance.OPTED_IN else FAILED, reason=text)
		frappe.db.set_value(RECIPIENT, r.name, values, update_modified=False)
		frappe.db.commit()
	refresh_counts(name)


def _stop(doc, status: str, note: str):
	frappe.db.set_value(CAMPAIGN, doc.name, {"status": status, "last_error": note})
	refresh_counts(doc.name)
	frappe.db.commit()


def refresh_counts(name: str):
	rows = frappe.db.sql(f"SELECT status, COUNT(*) FROM `tab{RECIPIENT}` WHERE parent = %s GROUP BY status", (name,))
	counts = {s: c for s, c in rows}
	values = {
		"total": sum(counts.values()),
		"eligible": counts.get(PENDING, 0) + sum(counts.get(s, 0) for s in (SENT, DELIVERED, READ, FAILED)),
		"excluded": counts.get(EXCLUDED, 0) + counts.get(REMOVED, 0),
		"sent": sum(counts.get(s, 0) for s in (SENT, DELIVERED, READ)),
		"delivered": counts.get(DELIVERED, 0) + counts.get(READ, 0),
		"read_count": counts.get(READ, 0),
		"failed": counts.get(FAILED, 0),
	}
	frappe.db.set_value(CAMPAIGN, name, values, update_modified=False)


def on_message_status(doc):
	"""WhatsApp Message on_update: delivered / read / failed ticks of campaign messages."""
	if doc.get("reference_doctype") != CAMPAIGN or not doc.get("reference_name"):
		return
	status = STATUS_FROM_META.get((doc.get("status") or "").lower())
	if not status:
		return
	row = frappe.db.get_value(RECIPIENT, {"parent": doc.reference_name, "message": doc.name}, ["name", "status"],
		as_dict=True)
	if not row or STATUS_RANK.get(status, 0) <= STATUS_RANK.get(row.status, 0) and status != FAILED:
		return
	values = {"status": status}
	if status == FAILED:
		values["reason"] = (doc.get("butler_error") or "")[:140]
	frappe.db.set_value(RECIPIENT, row.name, values, update_modified=False)
	refresh_counts(doc.reference_name)


@frappe.whitelist()
def marketing_templates(doctype=None, txt="", searchfield=None, start=0, page_len=20, filters=None):
	"""Link query of the Template field: approved, switched-on MARKETING templates."""
	return frappe.db.sql(
		"""SELECT name, actual_name, language_code FROM `tabWhatsApp Templates`
		WHERE status = 'APPROVED' AND UPPER(IFNULL(category, '')) = 'MARKETING'
			AND IFNULL(butler_enabled, 1) = 1 AND (name LIKE %(t)s OR actual_name LIKE %(t)s)
		ORDER BY actual_name LIMIT %(s)s, %(l)s""",
		{"t": f"%{txt}%", "s": cint(start), "l": cint(page_len)},
	)


@frappe.whitelist()
def template_preview(template: str) -> dict:
	frappe.has_permission("WhatsApp Templates", "read", throw=True)
	tpl = _template(template)
	try:
		check_template(tpl)
		problem = None
	except Exception as e:
		problem = frappe.utils.strip_html(str(e))
	return {"text": tpl.template, "footer": tpl.get("footer"), "header_type": tpl.header_type,
		"variables": count_body_variables(tpl.template), "problem": problem}
