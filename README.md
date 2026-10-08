# Butler WhatsApp

WhatsApp for ERPNext on top of `frappe_whatsapp` (Meta WhatsApp Cloud API).

- **Inbox** (`/desk/whatsapp-inbox`): conversations by customer number, sent/received files,
  who read each incoming message, sender + delivery ticks for outgoing messages, convert or link
  a conversation to a Lead / Customer, automatic match by phone (last 9 digits).
- **Send via WhatsApp** on Quotation, Butler Quotation, Sales Order and Sales Invoice (PDF).
- **Web Push** notifications for phones (iOS home-screen app, Android) and desktops.
- Role **WhatsApp Agent** for sales staff.

Credentials stay in the `WhatsApp Account` DocType (Password fields) of frappe_whatsapp.

Tests: `python -m pytest tests` (frappe is stubbed, no bench needed).
