"""butler_whatsapp.inbox helpers with a stubbed frappe module."""

import sys
import types

import pytest


class _AD(dict):
	__getattr__ = dict.get

	def __setattr__(self, k, v):
		self[k] = v


class _Msg(_AD):
	def has_value_changed(self, field):
		return field in self.get("_changed", ())


@pytest.fixture()
def inbox(monkeypatch):
	fr = types.ModuleType("frappe")
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda v: int(v or 0)
	utils.get_datetime = lambda v: v
	utils.get_fullname = lambda u: {"a@x": "Ahmed"}.get(u, u)
	utils.now_datetime = lambda: "now"
	utils.strip_html = lambda s: s
	utils.fmt_money = lambda v, **k: str(v)
	utils.formatdate = lambda v, *a, **k: str(v)
	fr.utils = utils
	fr._ = lambda s: s
	fr._dict = _AD
	fr.whitelist = lambda *a, **k: (lambda f: f)
	fr.set_values, fr.published, fr.saved, fr.errors = [], [], [], []
	fr.files = {}

	def set_value(dt, name, values, val=None, update_modified=True):
		fr.set_values.append((dt, name, values if val is None else {values: val}))

	def get_value(dt, filters, fields=None, **kw):
		if dt == "File":
			return fr.files.get(filters.get("file_url"))
		return None

	fr.db = types.SimpleNamespace(set_value=set_value, get_value=get_value)

	class FileDoc(_AD):
		def save(self, ignore_permissions=False):
			self.file_url = self.file_url.replace("/files/", "/private/files/")
			fr.saved.append(self)

	fr.get_doc = lambda dt, name: FileDoc(name=name, file_url="/files/abc.jpg", is_private=0)
	fr.log_error = lambda **kw: fr.errors.append(kw)
	fr.get_traceback = lambda: "tb"
	monkeypatch.setitem(sys.modules, "frappe", fr)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	for m in ("butler_whatsapp.inbox", "butler_whatsapp.api", "butler_whatsapp.compliance"):
		sys.modules.pop(m, None)
	import butler_whatsapp.inbox as mod

	monkeypatch.setattr(mod, "publish", lambda num, kind, name=None: fr.published.append((num, kind, name)))
	return mod, fr


def test_auto_link_single_customer_and_lead(inbox):
	mod, fr = inbox
	got = mod._auto_link("C1", [
		{"doctype": "Customer", "name": "CUST-1"}, {"doctype": "Lead", "name": "L-1"},
		{"doctype": "Contact", "name": "X"},
	])
	assert got == {"butler_customer": "CUST-1", "butler_lead": "L-1"}
	assert fr.set_values == [("WhatsApp Contact", "C1", got)]


def test_auto_link_ambiguous_does_nothing(inbox):
	mod, fr = inbox
	got = mod._auto_link("C1", [{"doctype": "Customer", "name": "A"}, {"doctype": "Customer", "name": "B"}])
	assert got == {} and fr.set_values == []


def test_preview(inbox):
	mod, _ = inbox
	assert mod._preview({"content_type": "text", "message": "hello"}) == "hello"
	assert mod._preview({"content_type": "image", "message": "pic"}) == "📷 pic"
	assert mod._preview({"content_type": "document", "message": ""}) == "📄"
	assert mod._preview({"message_type": "Template", "template": "hi-ar", "message": ""}) == "Template: hi-ar"


def test_sender_names(inbox):
	mod, _ = inbox
	assert mod._sender("a@x") == "Ahmed"
	assert mod._sender("Administrator") == "System"
	assert mod._sender(None) == "System"


def test_incoming_media_made_private_and_published(inbox):
	mod, fr = inbox
	fr.files["/files/abc.jpg"] = "F1"
	doc = _Msg(name="M1", type="Incoming", attach="/files/abc.jpg", to=None, _changed={"attach"})
	doc["from"] = "966500000001"
	mod.on_message_update(doc)
	assert fr.saved and fr.saved[0].is_private == 1 and fr.saved[0].attached_to_field == "attach"
	assert doc.attach == "/private/files/abc.jpg"
	assert fr.published == [("966500000001", "media", "M1")]


def test_outgoing_status_change_published(inbox):
	mod, fr = inbox
	doc = _Msg(name="M2", type="Outgoing", to="+966500000001", attach="/private/files/q.pdf", _changed={"status"})
	mod.on_message_update(doc)
	assert fr.saved == []  # outgoing / already private files are left alone
	assert fr.published == [("966500000001", "status", "M2")]


def test_unchanged_message_not_published(inbox):
	mod, fr = inbox
	doc = _Msg(name="M3", type="Outgoing", to="966500000001", _changed=set())
	mod.on_message_update(doc)
	assert fr.published == []
