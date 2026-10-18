"""Mobile numbers for WhatsApp: import from ERPNext or Excel, and a review list.

Nothing is merged or renamed silently when there is a doubt:
- the same number written twice in an Excel file with different names,
- a number that already exists under another name,
- an invalid number or one without a country code,
go to "WhatsApp Number Review" where a person decides (Add / Use this name /
Keep existing / Ignore).

Records of ERPNext (staff, customers, contacts, leads) are linked to the
WhatsApp number they own (a number can be a customer and a lead at once).
Consents are only recorded from a file when the user confirms that the people
agreed in writing (Meta policy) and says where the proof is kept.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime

from butler_whatsapp.meta import normalize_msisdn

CONTACT = "WhatsApp Contact"
IMPORT = "WhatsApp Number Import"
REVIEW = "WhatsApp Number Review"

ISSUE_REPEATED = "Repeated in the import"
ISSUE_OTHER_NAME = "Number exists with another name"
ISSUE_INVALID = "Invalid number"
ISSUE_NO_CC = "No country code"

PENDING, ADD, USE_NAME, KEEP, IGNORE = "Pending", "Add", "Use this name", "Keep existing", "Ignore"

# which link field of WhatsApp Contact each ERPNext doctype fills
LINK_FIELD = {"Customer": "butler_customer", "Lead": "butler_lead", "User": "butler_user"}
# name precedence when several ERPNext records share one number
NAME_RANK = {"Customer": 0, "Contact": 1, "Lead": 2, "User": 3, "Excel": 4}

YES = {"yes", "y", "1", "true", "نعم", "موافق", "موافقة", "agreed", "opted in", "x", "✓", "✔"}

NUMBER_HINTS = ("جوال", "رقم", "هاتف", "واتس", "mobile", "phone", "number", "whatsapp", "tel")
NAME_HINTS = ("اسم", "الاسم", "name", "customer", "عميل")
CONSENT_HINTS = ("موافق", "consent", "opt")
DATE_HINTS = ("تاريخ", "date")


# ------------------------------------------------------------------ helpers


def clean_name(value) -> str:
	return re.sub(r"\s+", " ", str(value or "")).strip()


def same_name(a, b) -> bool:
	return clean_name(a).casefold() == clean_name(b).casefold()


def is_placeholder_name(name, number) -> bool:
	"""A WhatsApp Contact named after its own number (or unnamed) has no real name."""
	n = re.sub(r"\D", "", str(name or ""))
	return not clean_name(name) or (n and n == re.sub(r"\D", "", str(number or "")))


def cell_text(value) -> str:
	"""Excel gives numbers as float (966501234567.0); keep the digits."""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	return str(value if value is not None else "").strip()


def check_number(raw) -> tuple[str | None, str | None]:
	"""(normalized number, issue). Saudi local formats (05..., 5...) get 966."""
	text = cell_text(raw)
	digits = re.sub(r"\D", "", text)
	if not digits:
		return None, ISSUE_INVALID
	num = normalize_msisdn(text)
	if num:
		return num, None
	if 6 <= len(digits) <= 9:
		return None, ISSUE_NO_CC
	return None, ISSUE_INVALID


def is_yes(value) -> bool:
	return clean_name(cell_text(value)).casefold() in YES


def guess_column(headers: list[str], hints: tuple) -> str | None:
	for h in headers:
		low = clean_name(h).casefold()
		if any(k in low for k in hints):
			return h
	return None


# ------------------------------------------------------------ classification


def classify(rows: list[dict], existing: dict, merge_records: bool) -> dict:
	"""Decide what to do with candidate rows.

	rows: {"raw", "name", "doctype", "docname", "links": {field: value}, "consent", "consent_date"}
	existing: {number: {"name": contact, "contact_name", fields of links ...}}
	merge_records: True for ERPNext records (a customer and a lead may share a number),
		False for a file (a number written twice with different names needs a decision).

	Returns {"add": [...], "update": [...], "same": [...], "review": [...]}; every entry
	is a dict with "number" and the rows it came from.
	"""
	out = {"add": [], "update": [], "same": [], "review": []}
	groups: dict[str, list[dict]] = {}
	for row in rows:
		num, issue = check_number(row.get("raw"))
		if issue:
			out["review"].append({**row, "number": cell_text(row.get("raw")), "issue": issue})
			continue
		groups.setdefault(num, []).append({**row, "number": num})

	for num, group in groups.items():
		names = []
		for r in sorted(group, key=lambda r: NAME_RANK.get(r.get("doctype") or "Excel", 9)):
			if clean_name(r.get("name")) and not any(same_name(r["name"], n) for n in names):
				names.append(clean_name(r["name"]))
		links: dict[str, str] = {}
		clash = False
		for r in group:
			for field, value in (r.get("links") or {}).items():
				if not value:
					continue
				if links.get(field) and links[field] != value:
					clash = True  # e.g. two customers with the same mobile
				links.setdefault(field, value)
		consent = next((r for r in group if r.get("consent")), None)

		if (not merge_records and len(names) > 1) or clash:
			for r in group:
				out["review"].append({**r, "issue": ISSUE_REPEATED,
					"others": [o for o in group if o is not r]})
			continue

		name = names[0] if names else ""
		ex = existing.get(num)
		if not ex:
			out["add"].append({"number": num, "name": name, "links": links, "rows": group, "consent": consent})
			continue
		values = {}
		if name and is_placeholder_name(ex.get("contact_name"), num):
			values["contact_name"] = name
		elif name and not same_name(name, ex.get("contact_name")) and not merge_records:
			for r in group:
				out["review"].append({**r, "issue": ISSUE_OTHER_NAME, "existing": ex})
			continue
		for field, value in links.items():
			if not ex.get(field):
				values[field] = value
		entry = {"number": num, "contact": ex["name"], "values": values, "rows": group, "consent": consent}
		out["update" if values else "same"].append(entry)
	return out


# ----------------------------------------------------------- ERPNext sources


def _user_rows(only: str | None = None) -> list[dict]:
	rows = []
	filters = {"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]}
	if only:
		filters["name"] = only
	for u in frappe.get_all("User", filters=filters, fields=["name", "full_name", "mobile_no", "phone"],
		limit_page_length=0):
		for raw in {u.mobile_no, u.phone} - {None, ""}:
			rows.append({"raw": raw, "name": u.full_name, "doctype": "User", "docname": u.name,
				"links": {"butler_user": u.name}})
	return rows


def _customer_rows(only: str | None = None) -> list[dict]:
	filters = {"disabled": 0, "mobile_no": ["is", "set"]}
	if only:
		filters["name"] = only
	return [
		{"raw": c.mobile_no, "name": c.customer_name, "doctype": "Customer", "docname": c.name,
		 "links": {"butler_customer": c.name}}
		for c in frappe.get_all("Customer", filters=filters, fields=["name", "customer_name", "mobile_no"],
			limit_page_length=0)
	]


def _lead_rows(only: str | None = None) -> list[dict]:
	rows = []
	fields = [f for f in ("mobile_no", "whatsapp_no", "phone") if frappe.db.has_column("Lead", f)]
	for lead in frappe.get_all("Lead", filters={"name": only} if only else {},
		fields=["name", "lead_name", "company_name"] + fields, limit_page_length=0):
		for raw in {lead.get(f) for f in fields} - {None, ""}:
			rows.append({"raw": raw, "name": lead.lead_name or lead.company_name, "doctype": "Lead",
				"docname": lead.name, "links": {"butler_lead": lead.name}})
	return rows


def _contact_rows(only: str | None = None) -> list[dict]:
	rows = []
	data = frappe.db.sql(
		"""SELECT c.name, c.full_name, cp.phone,
			(SELECT GROUP_CONCAT(CONCAT(dl.link_doctype, '::', dl.link_name)) FROM `tabDynamic Link` dl
			 WHERE dl.parenttype = 'Contact' AND dl.parent = c.name AND dl.link_doctype IN ('Customer', 'Lead')) AS links
		FROM `tabContact` c JOIN `tabContact Phone` cp ON cp.parent = c.name AND cp.parenttype = 'Contact'
		WHERE IFNULL(cp.phone, '') != '' AND (%(only)s IS NULL OR c.name = %(only)s)""",
		{"only": only}, as_dict=True,
	)
	for r in data:
		links = {}
		for pair in (r.links or "").split(","):
			if "::" in pair:
				dt, dn = pair.split("::", 1)
				links.setdefault(LINK_FIELD[dt], dn)
		rows.append({"raw": r.phone, "name": r.full_name, "doctype": "Contact", "docname": r.name, "links": links})
	return rows


def erpnext_rows(users=1, customers=1, contacts=1, leads=1) -> list[dict]:
	rows = []
	if cint(users):
		rows += _user_rows()
	if cint(customers):
		rows += _customer_rows()
	if cint(contacts):
		rows += _contact_rows()
	if cint(leads):
		rows += _lead_rows()
	return rows


# --------------------------------------------------------------- Excel / CSV


def read_table(file_url: str) -> list[list]:
	"""All rows of the first sheet of an .xlsx or a .csv file."""
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		frappe.throw(_("Attach the file again"))
	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	ext = (file_doc.file_name or file_url).rsplit(".", 1)[-1].lower()
	if ext in ("xlsx", "xlsm"):
		from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

		return read_xlsx_file_from_attached_file(fcontent=content) or []
	if ext == "csv":
		from frappe.utils.csvutils import read_csv_content

		if isinstance(content, bytes):
			content = content.decode("utf-8-sig", errors="replace")
		return read_csv_content(content) or []
	frappe.throw(_("Use an .xlsx or .csv file (save the Excel file as .xlsx)."))


def file_rows(doc) -> list[dict]:
	table = read_table(doc.file)
	if not table:
		frappe.throw(_("The file is empty"))
	headers = [clean_name(cell_text(h)) for h in table[0]]

	def col(title):
		if not title:
			return None
		for i, h in enumerate(headers):
			if same_name(h, title):
				return i
		frappe.throw(_("Column {0} is not in the file").format(title))

	num_i, name_i = col(doc.number_column), col(doc.name_column)
	use_consent = cint(doc.consent_confirmed) and doc.consent_column
	consent_i = col(doc.consent_column) if use_consent else None
	date_i = col(doc.consent_date_column) if use_consent and doc.consent_date_column else None
	rows = []
	for line in table[1:]:
		def at(i):
			return line[i] if i is not None and i < len(line) else None

		raw = cell_text(at(num_i))
		if not raw:
			continue
		consent = bool(use_consent and is_yes(at(consent_i)))
		date = None
		if consent and date_i is not None and at(date_i):
			try:
				date = getdate(at(date_i))
			except Exception:
				date = None
		rows.append({"raw": raw, "name": clean_name(cell_text(at(name_i))), "doctype": None, "docname": None,
			"links": {}, "consent": consent, "consent_date": date})
	return rows


# -------------------------------------------------------------- import form


def validate_import(doc):
	if not doc.title:
		doc.title = _("Numbers from ERPNext") if doc.source == "ERPNext" else _("Numbers from a file")
	if doc.source == "Excel" and doc.file and doc.has_value_changed("file"):
		table = read_table(doc.file)
		headers = [clean_name(cell_text(h)) for h in (table[0] if table else [])]
		doc.columns_found = "\n".join(h for h in headers if h)
		doc.number_column = doc.number_column or guess_column(headers, NUMBER_HINTS)
		doc.name_column = doc.name_column or guess_column(headers, NAME_HINTS)
		doc.consent_column = doc.consent_column or guess_column(headers, CONSENT_HINTS)
		doc.consent_date_column = doc.consent_date_column or guess_column(headers, DATE_HINTS)
	if cint(doc.consent_confirmed) and doc.consent_column:
		if not doc.consent_source:
			frappe.throw(_("Choose how these people agreed"))
		if not clean_name(doc.consent_evidence):
			frappe.throw(_("Write where the proof of the consents is kept"))


@frappe.whitelist(methods=["POST"])
def run_import(name: str) -> dict:
	frappe.only_for("System Manager")
	doc = frappe.get_doc(IMPORT, name)
	if doc.status in ("Queued",):
		frappe.throw(_("This import is already running"))
	if doc.source == "Excel" and not (doc.file and doc.number_column):
		frappe.throw(_("Attach the file and choose the number column"))
	doc.db_set("status", "Queued")
	frappe.enqueue("butler_whatsapp.numbers.import_job", queue="long", timeout=3600, name=name,
		enqueue_after_commit=True, job_id=f"butler_wa_import::{name}", deduplicate=True)
	return {"status": "Queued"}


def _existing() -> dict:
	cols = ["name", "contact_name", "mobile_no"] + [
		f for f in ("butler_customer", "butler_lead", "butler_user") if frappe.db.has_column(CONTACT, f)
	]
	out = {}
	for c in frappe.get_all(CONTACT, fields=cols, limit_page_length=0):
		num = normalize_msisdn(c.mobile_no)
		if num and num not in out:
			out[num] = c
	return out


def import_job(name: str):
	frappe.set_user("Administrator")
	doc = frappe.get_doc(IMPORT, name)
	try:
		if doc.source == "Excel":
			rows = file_rows(doc)
			merge = False
		else:
			rows = erpnext_rows(doc.from_users, doc.from_customers, doc.from_contacts, doc.from_leads)
			merge = True
		result = classify(rows, _existing(), merge)
		counts = apply_result(result, doc)
		counts["total_rows"] = len(rows)
		log = [
			_("Numbers read: {0}").format(len(rows)),
			_("Added: {0}").format(counts["added"]),
			_("Updated: {0}").format(counts["updated"]),
			_("Already up to date: {0}").format(counts["unchanged"]),
			_("Sent to review: {0}").format(counts["to_review"]),
			_("Consents recorded: {0}").format(counts["consents"]),
		]
		doc.reload()
		doc.update({**counts, "status": "Done", "log": "\n".join(log)})
		doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value(IMPORT, name, {"status": "Failed", "log": frappe.get_traceback()[-2000:]})
	frappe.db.commit()


def _consent_from(doc, entry) -> dict | None:
	row = entry.get("consent")
	if not row or not cint(doc.get("consent_confirmed")):
		return None
	return {"source": doc.consent_source, "date": row.get("consent_date"), "evidence": doc.consent_evidence,
		"import": doc.name}


def apply_result(result: dict, doc) -> dict:
	counts = {"added": 0, "updated": 0, "unchanged": 0, "to_review": 0, "consents": 0}
	source_label = _("Import {0}").format(doc.name)
	for entry in result["add"]:
		values = {"contact_name": entry["name"] or entry["number"], **entry["links"],
			"butler_source": _source_text(entry["rows"]) or source_label, "butler_import": doc.name}
		create_contact(entry["number"], values)
		counts["added"] += 1
		if record_import_consent(entry["number"], _consent_from(doc, entry)):
			counts["consents"] += 1
	for entry in result["update"]:
		frappe.db.set_value(CONTACT, entry["contact"], {**entry["values"], "butler_import": doc.name})
		counts["updated"] += 1
		if record_import_consent(entry["number"], _consent_from(doc, entry)):
			counts["consents"] += 1
	for entry in result["same"]:
		frappe.db.set_value(CONTACT, entry["contact"], "butler_import", doc.name, update_modified=False)
		counts["unchanged"] += 1
		if record_import_consent(entry["number"], _consent_from(doc, entry)):
			counts["consents"] += 1
	for row in result["review"]:
		add_review(row, doc)
		counts["to_review"] += 1
	return counts


def _source_text(rows: list[dict]) -> str:
	parts = []
	for r in rows:
		if r.get("doctype"):
			parts.append(f"{r['doctype']}: {r.get('docname')}")
	return ", ".join(dict.fromkeys(parts))[:140]


def create_contact(number: str, values: dict) -> str:
	doc = frappe.get_doc({"doctype": CONTACT, "mobile_no": number, "is_read": 1, **values})
	doc.insert(ignore_permissions=True)
	return doc.name


def record_import_consent(number: str, consent: dict | None) -> bool:
	"""Consent from a file / review row; never overrides an opt-out."""
	if not consent:
		return False
	from butler_whatsapp import compliance

	if compliance.get_consent(number)["state"] in (compliance.OPTED_IN, compliance.OPTED_OUT):
		return False
	note = _("Proof kept at: {0}").format(consent.get("evidence") or "-")
	if consent.get("date"):
		note += " — " + _("Consent date: {0}").format(frappe.utils.formatdate(consent["date"]))
	if consent.get("import"):
		note += " — " + _("Import {0}").format(consent["import"])
	compliance._record_consent(number, compliance.OPTED_IN, consent.get("source") or "Other",
		frappe.session.user, note, evidence=consent.get("evidence"))
	return True


def add_review(row: dict, doc=None):
	ex = row.get("existing") or {}
	others = row.get("others") or []
	other_text = "\n".join(
		f"{clean_name(o.get('name')) or '-'} ({o.get('doctype') or _('file')}{': ' + o['docname'] if o.get('docname') else ''})"
		for o in others
	)
	values = {
		"doctype": REVIEW,
		"number": row.get("number"),
		"raw_number": cell_text(row.get("raw")),
		"contact_name": clean_name(row.get("name")),
		"issue": row["issue"],
		"decision": PENDING,
		"existing_contact": ex.get("name"),
		"existing_name": ex.get("contact_name"),
		"source_doctype": row.get("doctype"),
		"source_name": row.get("docname"),
		"number_import": doc.name if doc else None,
		"other_rows": other_text,
		"consent": 1 if row.get("consent") else 0,
		"consent_source": doc.consent_source if (doc and row.get("consent")) else None,
		"consent_date": row.get("consent_date"),
	}
	# the same open question is not asked twice
	if frappe.db.exists(REVIEW, {"number": values["number"], "contact_name": values["contact_name"],
		"issue": values["issue"], "decision": PENDING}):
		return
	frappe.get_doc(values).insert(ignore_permissions=True)


# ------------------------------------------------------------------- review


def validate_review(doc):
	if not doc.has_value_changed("decision") or doc.decision == PENDING:
		return
	if doc.is_new():
		return
	if doc.decision == IGNORE:
		_mark_decided(doc)
		return
	num, issue = check_number(doc.number)
	if issue and doc.decision in (ADD, USE_NAME):
		frappe.throw(_("Correct the number first (with the country code, e.g. 9665XXXXXXXX)."))
	contact = frappe.db.get_value(CONTACT, {"mobile_no": ["in", [num, "+" + num]]}, "name") if num else None
	links = {}
	if doc.source_doctype in LINK_FIELD and doc.source_name:
		links[LINK_FIELD[doc.source_doctype]] = doc.source_name
	if doc.decision == ADD:
		if contact:
			frappe.throw(_("This number already exists ({0}). Choose \"Use this name\" or \"Keep existing\".").format(
				frappe.db.get_value(CONTACT, contact, "contact_name") or contact))
		create_contact(num, {"contact_name": doc.contact_name or num, **links, "butler_import": doc.number_import,
			"butler_source": _("Number review")})
	elif doc.decision == USE_NAME:
		if contact:
			frappe.db.set_value(CONTACT, contact, {"contact_name": doc.contact_name or num, **_empty_links(contact, links)})
		else:
			create_contact(num, {"contact_name": doc.contact_name or num, **links, "butler_source": _("Number review")})
	elif doc.decision == KEEP:
		if contact and links:
			frappe.db.set_value(CONTACT, contact, _empty_links(contact, links))
	if num and cint(doc.consent):
		imp = frappe.db.get_value(IMPORT, doc.number_import, ["consent_evidence"], as_dict=True) if doc.number_import else None
		record_import_consent(num, {"source": doc.consent_source, "date": doc.consent_date,
			"evidence": imp.consent_evidence if imp else None, "import": doc.number_import})
	_mark_decided(doc)


def _empty_links(contact: str, links: dict) -> dict:
	have = frappe.db.get_value(CONTACT, contact, list(links), as_dict=True) if links else {}
	return {k: v for k, v in links.items() if not (have or {}).get(k)}


def _mark_decided(doc):
	doc.decided_by = frappe.session.user
	doc.decided_at = now_datetime()


@frappe.whitelist(methods=["POST"])
def decide(names: str | list, decision: str) -> int:
	"""Bulk decision from the review list."""
	frappe.only_for("System Manager")
	names = frappe.parse_json(names) if isinstance(names, str) else names
	if decision not in (ADD, USE_NAME, KEEP, IGNORE):
		frappe.throw(_("Invalid decision"))
	done = 0
	for name in names:
		doc = frappe.get_doc(REVIEW, name)
		if doc.decision != PENDING:
			continue
		doc.decision = decision
		doc.save()
		done += 1
	return done


# --------------------------------------------------- keep numbers up to date


def on_record_update(doc, method=None):
	"""Customer / Lead / Contact / User saved: add or link its WhatsApp number."""
	if frappe.flags.in_import or frappe.flags.in_install or frappe.flags.in_migrate:
		return
	if doc.doctype == "User" and (doc.user_type != "System User" or not doc.enabled):
		return
	frappe.enqueue("butler_whatsapp.numbers.sync_record", queue="short", enqueue_after_commit=True,
		doctype=doc.doctype, name=doc.name)


def sync_record(doctype: str, name: str):
	if not frappe.db.exists(doctype, name):
		return
	rows = {"Customer": _customer_rows, "Lead": _lead_rows, "Contact": _contact_rows, "User": _user_rows}[doctype](name)
	if not rows:
		return
	nums = {check_number(r["raw"])[0] for r in rows} - {None}
	existing = {}
	for num in nums:
		c = frappe.db.get_value(CONTACT, {"mobile_no": ["in", [num, "+" + num]]},
			["name", "contact_name", "mobile_no", "butler_customer", "butler_lead", "butler_user"], as_dict=True)
		if c:
			existing[num] = c
	result = classify(rows, existing, merge_records=True)
	# invalid numbers of ERPNext records are not reported on every save
	result["review"] = [r for r in result["review"] if r["issue"] == ISSUE_REPEATED]
	for entry in result["add"]:
		create_contact(entry["number"], {"contact_name": entry["name"] or entry["number"], **entry["links"],
			"butler_source": _source_text(entry["rows"])})
	for entry in result["update"]:
		frappe.db.set_value(CONTACT, entry["contact"], entry["values"])
	for row in result["review"]:
		add_review(row)
	frappe.db.commit()


# ------------------------------------------------------------- contact names


@frappe.whitelist(methods=["POST"])
def rename_number(number: str, contact_name: str) -> dict:
	"""Give a WhatsApp number a name (inbox header / contact list)."""
	from butler_whatsapp.inbox import _contact_for_link, _require_number, publish

	num = _require_number(number, "write")
	contact = _contact_for_link(num)
	contact_name = clean_name(contact_name)
	if not contact_name:
		frappe.throw(_("Write a name"))
	frappe.db.set_value(CONTACT, contact, "contact_name", contact_name)
	publish(num, "info")
	return {"contact": contact, "contact_name": contact_name}
