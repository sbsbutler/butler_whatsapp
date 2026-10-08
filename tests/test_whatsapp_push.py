"""notify_users bookkeeping in butler_whatsapp.push with a stubbed frappe."""

import sys
import types

import pytest


class _AD(dict):
	__getattr__ = dict.get


@pytest.fixture()
def push(monkeypatch):
	fr = types.ModuleType("frappe")
	fr._ = lambda s: s
	fr.whitelist = lambda **kw: (lambda f: f)
	fr.utils = types.SimpleNamespace(get_url=lambda: "https://butler.com.sa")
	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = lambda: "NOW"
	utils.get_url = lambda: "https://butler.com.sa"
	pw = types.ModuleType("frappe.utils.password")
	fr.store = {}
	pw.get_decrypted_password = lambda d, n, f, raise_exception=True: fr.store.get((d, n, f))
	pw.set_encrypted_password = lambda d, n, v, f: fr.store.__setitem__((d, n, f), v)
	fr.subs, fr.updates, fr.deleted, fr.errors = [], [], [], []
	fr.get_all = lambda dt, filters=None, fields=None, pluck=None: fr.subs
	fr.db = types.SimpleNamespace(set_value=lambda dt, n, v, update_modified=True: fr.updates.append((n, v)))
	fr.delete_doc = lambda dt, n, **kw: fr.deleted.append(n)
	fr.log_error = lambda **kw: fr.errors.append(kw)
	fr.get_traceback = lambda: "tb"
	wz = types.ModuleType("werkzeug.wrappers")
	wz.Response = object
	for name, mod in {"frappe": fr, "frappe.utils": utils, "frappe.utils.password": pw,
			"werkzeug.wrappers": wz}.items():
		monkeypatch.setitem(sys.modules, name, mod)
	sys.modules.pop("butler_whatsapp.push", None)
	import butler_whatsapp.push as mod
	return mod, fr


def test_key_generated_once_and_reused(push):
	mod, fr = push
	k1 = mod._vapid_private()
	assert mod._vapid_private() == k1 and len(fr.store) == 1


def test_notify_users_bookkeeping(push, monkeypatch):
	mod, fr = push
	fr.subs = [
		_AD(name="ok", endpoint="https://fcm.googleapis.com/a", p256dh="x", auth="y", failures=2),
		_AD(name="gone", endpoint="https://web.push.apple.com/b", p256dh="x", auth="y", failures=0),
		_AD(name="bad", endpoint="https://fcm.googleapis.com/c", p256dh="x", auth="y", failures=4),
	]

	def fake_send(sub, data, key, subject):
		assert subject == "https://butler.com.sa" and data["title"] == "T"
		if sub["endpoint"].endswith("/b"):
			raise mod.webpush.Gone(410)
		if sub["endpoint"].endswith("/c"):
			raise RuntimeError("push service HTTP 500")
		return 201

	monkeypatch.setattr(mod.webpush, "send", fake_send)
	result = mod.notify_users(["u@x"], {"title": "T", "body": "B"})
	assert result == {"devices": 3, "sent": 1}
	assert ("ok", {"last_success": "NOW", "failures": 0}) in fr.updates
	assert fr.deleted == ["gone"]
	assert ("bad", {"failures": 5, "enabled": 0}) in fr.updates  # 5th failure disables the device
	assert len(fr.errors) == 1


def test_no_users_no_work(push):
	mod, fr = push
	assert mod.notify_users([], {"title": "T"}) == {"devices": 0, "sent": 0}
