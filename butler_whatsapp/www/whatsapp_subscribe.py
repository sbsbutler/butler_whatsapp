# Copyright (c) 2026, Butler and contributors
"""/whatsapp-subscribe: customers agree to receive WhatsApp messages from Butler (Meta opt-in).
?k=<token>  personal link sent by e-mail (number already known)
?paper=1    printable consent form for the showroom
"""

import frappe

no_cache = 1


def get_context(context):
	from butler_whatsapp.optin import page_context

	args = frappe.form_dict
	context.update(page_context(args.get("k")))
	context.paper = bool(frappe.utils.cint(args.get("paper")))
	context.no_header = context.paper
	context.no_footer = context.paper
	context.title = "اشتراك في رسائل باتلر عبر واتساب"
	context.show_sidebar = False
	return context
