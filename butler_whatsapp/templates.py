"""Template helpers shared by the inbox, document sending and appointments."""

from __future__ import annotations

import frappe

from butler_whatsapp.compliance import template_is_enabled

TEMPLATES = "WhatsApp Templates"


def localized(template: str | None, lang: str | None) -> str | None:
	"""The same template (same name at Meta) in `lang` when it is approved and
	switched on; otherwise the given template unchanged."""
	if not template or not lang:
		return template
	row = frappe.db.get_value(TEMPLATES, template, ["actual_name", "template_name", "language_code"], as_dict=True)
	if not row:
		return template
	if (row.language_code or "").lower().startswith(lang.lower()):
		return template
	actual = row.actual_name or row.template_name
	for r in frappe.get_all(
		TEMPLATES,
		filters={"status": "APPROVED"},
		or_filters={"actual_name": actual, "template_name": actual},
		fields=["name", "language_code", "butler_enabled"],
	):
		if (r.language_code or "").lower().startswith(lang.lower()) and template_is_enabled(r):
			return r.name
	return template
