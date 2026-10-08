"""Broadcast logic of butler_whatsapp.realtime with a stubbed frappe module."""

import sys
import types
from datetime import datetime

import pytest


class _AD(dict):
	__getattr__ = dict.get


@pytest.fixture()
def rt(monkeypatch):
	fr = types.ModuleType("frappe")
	utils = types.ModuleType("frappe.utils")
	utils.now = lambda: "2026-10-08 17:00:00"
	utils.get_datetime = lambda v: v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
	utils.strip_html = lambda s: s
	fr.utils = utils
	fr.published, fr.callbacks, fr.errors, fr.jobs = [], [], [], []
	fr.enqueue = lambda method, **kw: fr.jobs.append((method, kw))
	fr.db = types.SimpleNamespace(after_commit=types.SimpleNamespace(add=fr.callbacks.append))
	fr.state = {"contact": None, "msg": None}

	def get_value(dt, filters, fields, as_dict=False):
		return fr.state["contact"]

	def get_all(dt, filters=None, pluck=None, distinct=False):
		return {"DocPerm": ["System Manager"], "Custom DocPerm": [], "Has Role": ["a@x", "b@x"],
			"User": ["a@x", "b@x"]}[dt]

	fr.db.get_value = get_value
	fr.get_all = get_all
	fr.get_doc = lambda dt, name: fr.state["msg"]
	fr.publish_realtime = lambda event, payload, user=None: fr.published.append((event, user, payload))
	fr.log_error = lambda **kw: fr.errors.append(kw)
	fr.get_traceback = lambda: "tb"
	monkeypatch.setitem(sys.modules, "frappe", fr)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	fr._ = lambda s: s
	fake_inbox = types.ModuleType("butler_whatsapp.inbox")
	fake_inbox.on_incoming = lambda doc: None
	fake_inbox.publish = lambda *a, **k: None
	monkeypatch.setitem(sys.modules, "butler_whatsapp.inbox", fake_inbox)
	sys.modules.pop("butler_whatsapp.realtime", None)
	sys.modules.pop("butler_whatsapp.access", None)
	import butler_whatsapp.realtime as mod
	return mod, fr


def _msg(**kw):
	base = {"name": "M1", "type": "Incoming", "from": "966500000001", "message": "hi",
		"content_type": "text", "profile_name": "Ali", "creation": datetime(2026, 10, 8, 17, 0, 0)}
	base.update(kw)
	return _AD(base)


def test_outgoing_only_refreshes_inbox(rt, monkeypatch):
	mod, fr = rt
	calls = []
	fake = types.ModuleType("butler_whatsapp.inbox")
	fake.publish = lambda num, kind, name=None: calls.append((num, kind, name))
	monkeypatch.setitem(sys.modules, "butler_whatsapp.inbox", fake)
	mod.on_message_insert(_msg(type="Outgoing", to="+966500000001", **{"from": None}))
	assert fr.callbacks == [] and fr.jobs == []
	assert calls == [("966500000001", "outgoing", "M1")]


def test_unassigned_contact_goes_to_all_agents(rt):
	mod, fr = rt
	fr.state["msg"] = _msg()
	fr.state["contact"] = _AD(name="ROOM1", contact_name="966500000001", email=None,
		creation=datetime(2026, 10, 1))
	mod.on_message_insert(fr.state["msg"])
	assert len(fr.callbacks) == 1
	assert fr.jobs == [("butler_whatsapp.push.notify_incoming",
		{"queue": "short", "enqueue_after_commit": True, "message": "M1"})]
	fr.callbacks[0]()
	events = [(e, u) for e, u, _ in fr.published]
	for user in ("a@x", "b@x"):
		assert ("latest_chat_updates", user) in events
		assert ("ROOM1", user) in events
		assert ("butler_wa_incoming", user) in events
	notif = [p for e, _, p in fr.published if e == "butler_wa_incoming"][0]
	assert notif["display"] == "Ali" and notif["new_contact"] is False and notif["room"] == "ROOM1"
	assert fr.errors == []


def test_assigned_contact_only_notifies_assignee(rt):
	mod, fr = rt
	fr.state["msg"] = _msg()
	fr.state["contact"] = _AD(name="R2", contact_name="x", email="owner@x", creation=datetime(2026, 10, 8, 17, 0, 5))
	mod.on_message_insert(fr.state["msg"])
	fr.callbacks[0]()
	# whatsapp_chat already pushes chat updates to the assignee; we only add the notification
	assert [(e, u) for e, u, _ in fr.published] == [("butler_wa_incoming", "owner@x")]
	assert fr.published[0][2]["new_contact"] is True
