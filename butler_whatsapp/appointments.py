"""WhatsApp confirmations and reminders for showroom visits (ERPNext "Appointment").

- A confirmation template is sent as soon as an appointment is booked: created
  by staff, or booked by the customer on the /book_appointment page (no need to
  wait for the e-mail verification), if enabled in the settings.
- A reminder template is sent N hours before the visit (hourly scheduler).
- Staff can (re)send the confirmation from the Appointment form.
Templates are picked in the customer's language when an approved version
exists (same name at Meta, different language).
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from butler_whatsapp import access
from butler_whatsapp.meta import normalize_msisdn

APPOINTMENT = "Appointment"
CONFIRM = "confirmation"
ACTIVE = ("Open", "Unverified")  # booked by staff / booked on the website
REMIND = "reminder"

AR_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ------------------------------------------------------------------ helpers


def format_day(dt, lang: str) -> str:
	"""'الأحد 18-10-2026' / 'Sunday 18-10-2026'."""
	days = AR_DAYS if lang == "ar" else EN_DAYS
	return f"{days[dt.weekday()]} {dt.strftime('%d-%m-%Y')}"


def format_time(dt, lang: str) -> str:
	"""'4:30 مساءً' / '4:30 PM'."""
	hour = dt.hour % 12 or 12
	if lang == "ar":
		suffix = "صباحاً" if dt.hour < 12 else "مساءً"
	else:
		suffix = "AM" if dt.hour < 12 else "PM"
	return f"{hour}:{dt.minute:02d} {suffix}"


def appointment_language(doc) -> str:
	if doc.get("appointment_with") in ("Customer", "Lead") and doc.get("party"):
		lang = (frappe.db.get_value(doc.appointment_with, doc.party, "language") or "").lower()
		if lang:
			return "ar" if lang.startswith("ar") else "en"
	from butler_whatsapp.compliance import script_language

	return script_language(doc.get("customer_name"))


def _number(doc) -> str | None:
	num = normalize_msisdn(doc.get("customer_phone_number"))
	if num:
		return num
	if doc.get("appointment_with") in ("Customer", "Lead") and doc.get("party"):
		mobile = frappe.db.get_value(doc.appointment_with, doc.party, "mobile_no")
		return normalize_msisdn(mobile)
	return None


def _template_for(kind: str, lang: str) -> str | None:
	from butler_whatsapp.templates import localized

	field = "appointment_confirm_template" if kind == CONFIRM else "appointment_reminder_template"
	return localized(access.settings().get(field), lang)


def body_values(doc, lang: str) -> list[str]:
	"""{{1}} name, {{2}} day + date, {{3}} time, {{4}} showroom location link."""
	when = get_datetime(doc.scheduled_time)
	location = (access.settings().get("showroom_location_url") or "").strip()
	return [doc.customer_name or "", format_day(when, lang), format_time(when, lang), location]


# --------------------------------------------------------------------- send


def send(name: str, kind: str = CONFIRM, raise_errors: bool = False) -> str | None:
	"""Send the confirmation / reminder of one appointment. Returns the message name."""
	from butler_whatsapp.inbox import send_template_core

	doc = frappe.get_doc(APPOINTMENT, name)
	field = "butler_wa_confirmation_sent" if kind == CONFIRM else "butler_wa_reminder_sent"
	try:
		num = _number(doc)
		if not num:
			frappe.throw(_("The appointment has no valid mobile number"))
		_record_booking_consent(doc, num)
		lang = appointment_language(doc)
		template = _template_for(kind, lang)
		if not template:
			frappe.throw(_("Choose the appointment templates in WhatsApp Inbox Settings"))
		tpl = frappe.get_doc("WhatsApp Templates", template)
		values = body_values(doc, lang)
		from butler_whatsapp.meta import count_body_variables

		values = values[: count_body_variables(tpl.template)]
		if any(not v for v in values):
			frappe.throw(_("Fill in the showroom location link in WhatsApp Inbox Settings"))
		message = send_template_core(num, tpl, values, reference=(APPOINTMENT, doc.name), booking=True)
	except Exception as e:
		frappe.db.set_value(APPOINTMENT, doc.name, "butler_wa_error", str(e)[:500], update_modified=False)
		if raise_errors:
			raise
		frappe.log_error(title=f"WhatsApp appointment {kind} failed", message=frappe.get_traceback())
		return None
	frappe.db.set_value(APPOINTMENT, doc.name, {field: now_datetime(), "butler_wa_error": ""}, update_modified=False)
	doc.add_comment(
		"Info",
		_("WhatsApp confirmation sent to {0}").format("+" + num) if kind == CONFIRM
		else _("WhatsApp reminder sent to {0}").format("+" + num),
	)
	return message


def _record_booking_consent(doc, num: str):
	"""Staff ticked "Customer agreed to WhatsApp messages" on the appointment."""
	from butler_whatsapp import compliance

	if not cint(doc.get("butler_wa_consent")):
		return
	state = compliance.get_consent(num)["state"]
	if state in (compliance.OPTED_IN, compliance.OPTED_OUT):
		return  # never override an opt-out
	compliance._record_consent(num, compliance.OPTED_IN, "Appointment booking", doc.owner,
		_("Appointment {0}").format(doc.name))


def send_job(name: str, kind: str = CONFIRM):
	frappe.set_user("Administrator")
	send(name, kind)
	frappe.db.commit()


@frappe.whitelist(methods=["POST"])
def send_confirmation(name: str) -> dict:
	"""Button on the Appointment form."""
	frappe.has_permission(APPOINTMENT, "write", name, throw=True)
	frappe.has_permission("WhatsApp Message", "create", throw=True)
	message = send(name, CONFIRM, raise_errors=True)
	return {"message": message}


# -------------------------------------------------------------- doc events


def _auto_enabled() -> bool:
	s = access.settings()
	value = s.get("appointment_auto_confirm")
	return (value is None or cint(value)) and bool(s.get("appointment_confirm_template"))


def on_update(doc, method=None):
	"""Appointment on_update (also runs after insert): confirm a new booking once.
	"Unverified" = booked on the website, e-mail not confirmed yet: still a real booking."""
	if doc.status not in ACTIVE or doc.get("butler_wa_confirmation_sent") or not _auto_enabled():
		return
	if get_datetime(doc.scheduled_time) <= now_datetime():
		return
	if doc.flags.butler_wa_queued:
		return
	doc.flags.butler_wa_queued = True
	frappe.enqueue(
		"butler_whatsapp.appointments.send_job", queue="short", enqueue_after_commit=True,
		name=doc.name, kind=CONFIRM,
	)


# --------------------------------------------------------------- reminders


def send_reminders():
	"""Scheduler (hourly): remind customers N hours before their visit."""
	s = access.settings()
	hours = cint(s.get("appointment_reminder_hours")) or 24
	if not s.get("appointment_reminder_template") or not frappe.db.has_column(APPOINTMENT, "butler_wa_reminder_sent"):
		return
	now = now_datetime()
	rows = frappe.get_all(
		APPOINTMENT,
		filters={
			"status": ["in", list(ACTIVE)],
			"scheduled_time": ["between", [now, now + timedelta(hours=hours)]],
			"butler_wa_reminder_sent": ["is", "not set"],
		},
		fields=["name", "scheduled_time", "butler_wa_confirmation_sent", "creation"],
	)
	for r in rows:
		start = get_datetime(r.scheduled_time) - timedelta(hours=hours)
		# booked inside the reminder window: the confirmation was enough
		if get_datetime(r.creation) >= start:
			continue
		if r.butler_wa_confirmation_sent and get_datetime(r.butler_wa_confirmation_sent) >= start:
			continue
		send(r.name, REMIND)
		frappe.db.commit()
