"""access.py rules with a stubbed frappe."""

import sys
import types

import pytest


@pytest.fixture()
def acc(monkeypatch):
	fr = types.ModuleType("frappe")
	fr._ = lambda s: s
	fr.session = types.SimpleNamespace(user="agent@x")
	fr.state = {"supervisors": ["boss@x"], "assignee": None, "notify": 0}

	class S(dict):
		def get(self, k, d=None):
			return {"supervisors": [types.SimpleNamespace(user=u) for u in fr.state["supervisors"]],
				"notify_supervisors": fr.state["notify"]}.get(k, d)

	fr.get_cached_doc = lambda dt: S()
	fr.db = types.SimpleNamespace(
		sql=lambda q, *a, **k: [[fr.state["assignee"]]] if fr.state["assignee"] else [],
		escape=lambda v: "'" + v + "'",
	)
	fr.utils = types.SimpleNamespace(cint=lambda v: int(v or 0))
	fr.PermissionError = PermissionError

	def throw(msg, exc=Exception):
		raise exc(msg)

	fr.throw = throw
	monkeypatch.setitem(sys.modules, "frappe", fr)
	sys.modules.pop("butler_whatsapp.access", None)
	import butler_whatsapp.access as mod

	return mod, fr


def test_supervisor_sees_all(acc):
	mod, fr = acc
	assert mod.is_supervisor("boss@x") and mod.is_supervisor("Administrator")
	assert not mod.is_supervisor("agent@x")
	assert mod.message_query("boss@x") == ""
	assert "butler_assigned_to = 'agent@x'" in mod.message_query("agent@x")
	assert mod.contact_query("agent@x") == "`tabWhatsApp Contact`.`butler_assigned_to` = 'agent@x'"


def test_no_supervisors_means_no_restriction(acc):
	mod, fr = acc
	fr.state["supervisors"] = []
	assert mod.is_supervisor("anyone@x")
	assert mod.message_query("anyone@x") == ""
	assert mod.notify_targets("966") is None


def test_agent_access_by_assignment(acc):
	mod, fr = acc
	assert not mod.can_access("966500", "agent@x")
	with pytest.raises(PermissionError):
		mod.check_access("966500")
	fr.state["assignee"] = "agent@x"
	assert mod.can_access("+966500", "agent@x")
	assert mod.message_has_permission({"type": "Incoming", "from": "966500"}, "read", "agent@x") is None
	fr.state["assignee"] = "other@x"
	assert mod.message_has_permission({"type": "Incoming", "from": "966500"}, "read", "agent@x") is False
	assert mod.contact_has_permission({"butler_assigned_to": "agent@x"}, "read", "agent@x") is None
	assert mod.contact_has_permission({"butler_assigned_to": "x@x"}, "read", "agent@x") is False


def test_notify_targets(acc):
	mod, fr = acc
	assert mod.notify_targets("966") == ["boss@x"]  # unassigned -> supervisors
	fr.state["assignee"] = "agent@x"
	assert mod.notify_targets("966") == ["agent@x"]
	fr.state["notify"] = 1
	assert mod.notify_targets("966") == ["agent@x", "boss@x"]
