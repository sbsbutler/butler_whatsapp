"""Meta-policy helpers (pure) and compliance / automation rules with a stubbed frappe."""

import hashlib
import hmac
import sys
import types
from datetime import datetime, time, timedelta

import pytest

from butler_whatsapp import meta


# ------------------------------------------------------------------ pure helpers


def test_keywords_whole_message_only():
	kws = meta.keyword_list("إيقاف\nSTOP\nإلغاء الاشتراك")
	assert meta.matches_keyword(" ايقاف! ", kws)
	assert meta.matches_keyword("stop.", kws)
	assert meta.matches_keyword("الغاء الاشتراك", kws)
	assert not meta.matches_keyword("أبي إيقاف الطلب", kws)
	assert not meta.matches_keyword("", kws)


def test_template_rules():
	ok = meta.validate_template_text("مرحباً {{1}}، طلبك {{2}} جاهز.", ["أحمد", "SO-1"], footer="باتلر")
	assert ok == []
	bad = meta.validate_template_text("{{1}} مرحبا {{3}}", ["a,b"])
	assert any("متتالية" in p for p in bad)
	assert any("يبدأ" in p for p in bad)
	assert any("فاصلة" in p for p in bad)
	assert any("عدد الأمثلة" in p for p in bad)
	assert any("متجاورين" in p for p in meta.validate_template_text("أهلا {{1}}{{2}} شكرا", ["a", "b"]))
	assert any("التذييل" in p for p in meta.validate_template_text("نص", [], footer="x" * 61))


def test_signature():
	body = b'{"entry":[]}'
	sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
	assert meta.webhook_signature_ok("secret", body, sig)
	assert not meta.webhook_signature_ok("secret", body + b" ", sig)
	assert not meta.webhook_signature_ok("", body, sig)
	assert not meta.webhook_signature_ok("secret", body, None)


def test_errors_explained():
	assert "24" in meta.explain_error(131047)
	assert meta.explain_error("999", "Title", "Details") == "Title — Details (#999)"
	payload = {"entry": [{"changes": [{"value": {"statuses": [
		{"id": "wamid.1", "status": "failed", "errors": [{"code": 131026, "title": "Undeliverable"}]},
		{"id": "wamid.2", "status": "delivered"},
	]}}]}]}
	assert meta.errors_for_message(payload, "wamid.1")[0]["code"] == 131026
	assert meta.errors_for_message(payload, "wamid.2") == []


def test_business_hours_and_minutes():
	days = {6, 0, 1, 2, 3}  # Sun-Thu
	sun_10 = datetime(2026, 10, 11, 10, 0)  # Sunday
	assert meta.in_business_hours(sun_10, days, time(8), time(17))
	assert not meta.in_business_hours(sun_10.replace(hour=18), days, time(8), time(17))
	fri = datetime(2026, 10, 16, 10, 0)  # Friday
	assert not meta.in_business_hours(fri, days, time(8), time(17))
	# Thursday 16:30 -> Sunday 08:30 = 30 + 30 business minutes
	thu = datetime(2026, 10, 15, 16, 30)
	assert meta.business_minutes(thu, datetime(2026, 10, 18, 8, 30), days, time(8), time(17)) == 60
	assert meta.business_minutes(sun_10, sun_10 - timedelta(minutes=5), days, time(8), time(17)) == 0


class _Resp:
	def __init__(self, data, status=200):
		self._d, self.status_code = data, status

	def json(self):
		return self._d


def test_cloud_api_health_and_templates():
	calls = []

	class S:
		def get(self, url, headers=None, params=None, timeout=None):
			calls.append((url, params))
			if url.endswith("/PHONE"):
				return _Resp({"quality_rating": "GREEN", "messaging_limit_tier": "TIER_1K"})
			if "after=x" in url:
				return _Resp({"data": [{"name": "b"}]})
			return _Resp({"data": [{"name": "a"}], "paging": {"next": "https://g/next?after=x"}})

		def post(self, url, headers=None, json=None, timeout=None, **k):
			calls.append((url, json))
			return _Resp({"success": True})

	api = meta.CloudAPI("https://g", "v24.0", "PHONE", "tok", session=S())
	assert api.phone_info()["quality_rating"] == "GREEN"
	assert [t["name"] for t in api.list_templates("https://g/v24.0/WABA")] == ["a", "b"]
	assert api.mark_read("wamid.9")
	assert calls[-1][1]["status"] == "read"


def test_cloud_api_error_code():
	class S:
		def post(self, *a, **k):
			return _Resp({"error": {"code": 131047, "message": "Re-engagement message"}}, 400)

	api = meta.CloudAPI("https://g", "v24.0", "P", "t", session=S())
	with pytest.raises(meta.WhatsAppError) as e:
		api.send({})
	assert e.value.code == 131047 and "24" in str(e.value)


# ------------------------------------------------------- compliance (stubbed)


class _AD(dict):
	__getattr__ = dict.get

	def __setattr__(self, k, v):
		self[k] = v


@pytest.fixture()
def comp(monkeypatch):
	fr = types.ModuleType("frappe")
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda v: int(v or 0)
	utils.now_datetime = lambda: datetime(2026, 10, 11, 10, 0)
	utils.get_datetime = lambda v: v
	utils.get_time = lambda v: v
	fr.utils = utils
	fr._ = lambda s: s
	fr._dict = _AD
	fr.whitelist = lambda *a, **k: (lambda f: f)
	fr.state = _AD(settings={"require_opt_in": 1}, consent={}, last_in=None, has_incoming=False, sent=[],
		recorded=[], notified=[])

	class Throw(Exception):
		pass

	def throw(msg, *a, **k):
		raise Throw(msg)

	fr.throw, fr.Throw = throw, Throw
	fr.PermissionError = PermissionError

	def get_value(dt, name, fields=None, as_dict=False, **k):
		if dt == "WhatsApp Contact" and as_dict:
			return _AD(fr.state.consent) if fr.state.consent else None
		return None

	fr.db = types.SimpleNamespace(get_value=get_value, exists=lambda *a, **k: fr.state.has_incoming)
	monkeypatch.setitem(sys.modules, "frappe", fr)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)

	access = types.ModuleType("butler_whatsapp.access")
	access.settings = lambda: _AD(fr.state.settings)
	access.SETTINGS = "WhatsApp Inbox Settings"
	access.assignee = lambda num: None
	access.supervisors = lambda: ["boss@x"]
	access.is_supervisor = lambda u=None: False
	monkeypatch.setitem(sys.modules, "butler_whatsapp.access", access)
	import butler_whatsapp

	monkeypatch.setattr(butler_whatsapp, "access", access, raising=False)

	inbox = types.ModuleType("butler_whatsapp.inbox")
	inbox._contact = lambda num, create=False: "C1"
	monkeypatch.setitem(sys.modules, "butler_whatsapp.inbox", inbox)
	api = types.ModuleType("butler_whatsapp.api")
	api._last_incoming = lambda num: fr.state.last_in
	monkeypatch.setitem(sys.modules, "butler_whatsapp.api", api)

	for m in ("butler_whatsapp.compliance", "butler_whatsapp.automation"):
		sys.modules.pop(m, None)
	import butler_whatsapp.compliance as mod

	monkeypatch.setattr(mod, "_record_consent", lambda num, state, source, by, note=None: fr.state.recorded.append((num, state, source)))
	monkeypatch.setattr(mod, "enqueue_auto_text", lambda num, text, kind="auto": fr.state.sent.append((num, text)))
	monkeypatch.setattr(mod, "notify_users", lambda users, subject, url=None, document=None: fr.state.notified.append(subject))
	return mod, fr


def _tpl(**k):
	return _AD({"name": "T", "actual_name": "t", "butler_enabled": 1, **k})


def test_template_switched_off(comp):
	mod, fr = comp
	fr.state.consent = {"butler_consent": mod.OPTED_IN}
	with pytest.raises(fr.Throw):
		mod.check_template_send("966500", _tpl(butler_enabled=0))
	mod.check_template_send("966500", _tpl(butler_enabled=None))  # legacy rows count as on


def test_opt_in_required_outside_window(comp):
	mod, fr = comp
	with pytest.raises(fr.Throw):
		mod.check_template_send("966500", _tpl())
	fr.state.last_in = datetime(2026, 10, 11, 9, 0)  # wrote 1h ago -> service window
	mod.check_template_send("966500", _tpl())
	fr.state.last_in = None
	fr.state.consent = {"butler_consent": mod.OPTED_IN, "butler_consent_source": "نموذج الموقع"}
	mod.check_template_send("966500", _tpl())


def test_opted_out_blocks_even_in_window(comp):
	mod, fr = comp
	fr.state.consent = {"butler_consent": mod.OPTED_OUT}
	fr.state.last_in = datetime(2026, 10, 11, 9, 0)
	with pytest.raises(fr.Throw):
		mod.check_template_send("966500", _tpl())


def test_setting_off_or_incoming_counts(comp):
	mod, fr = comp
	fr.state.settings = {"require_opt_in": 0}
	mod.check_template_send("966500", _tpl())
	fr.state.settings = {"require_opt_in": 1, "incoming_is_opt_in": 1}
	fr.state.has_incoming = True
	mod.check_template_send("966500", _tpl())


def test_keywords_handled(comp):
	mod, fr = comp
	fr.state.settings = {"opt_out_keywords": "إيقاف\nSTOP", "opt_out_reply": "تم الإيقاف",
		"opt_in_keywords": "اشتراك", "opt_in_reply": "تم التفعيل"}
	assert mod.handle_keywords(_AD({"from": "966500000001", "message": "Stop", "content_type": "text"}))
	assert fr.state.recorded[-1][1] == mod.OPTED_OUT and fr.state.sent[-1][1] == "تم الإيقاف"
	assert fr.state.notified
	assert mod.handle_keywords(_AD({"from": "966500000001", "message": "اشتراك", "content_type": "text"}))
	assert fr.state.recorded[-1][1] == mod.OPTED_IN
	assert not mod.handle_keywords(_AD({"from": "966500000001", "message": "ابي ايقاف الطلب"}))
	assert not mod.handle_keywords(_AD({"from": "966500000001", "message": "STOP", "content_type": "image"}))


def test_template_status_sync(comp, monkeypatch):
	mod, fr = comp
	local = [_AD(name="T1", id="1", actual_name="a", language_code="ar", status="PENDING", category="UTILITY"),
		_AD(name="T2", id="2", actual_name="b", language_code="ar", status="APPROVED", category="UTILITY")]
	fr.get_all = lambda *a, **k: local
	updates = []
	fr.db.set_value = lambda dt, name, values, update_modified=True: updates.append((name, values))
	alerts = mod._apply_template_statuses([
		{"id": "1", "name": "a", "language": "ar", "status": "APPROVED", "category": "UTILITY"},
		{"id": "2", "name": "b", "language": "ar", "status": "PAUSED", "category": "MARKETING"},
		{"id": "9", "name": "zzz", "language": "ar", "status": "APPROVED"},
	], "ACC")
	assert ("T1", {"status": "APPROVED"}) in updates
	assert ("T2", {"status": "PAUSED", "category": "MARKETING"}) in updates
	assert len(alerts) == 3


def test_away_and_welcome(comp, monkeypatch):
	mod, fr = comp
	import butler_whatsapp.automation as auto

	sent = []
	monkeypatch.setattr(mod, "enqueue_auto_text", lambda num, text, kind="auto": sent.append(text))
	fr.db.count = lambda *a, **k: 1
	monkeypatch.setattr(auto, "_cooldown_passed", lambda num, hours: True)
	fr.state.settings = {"welcome_enabled": 1, "welcome_message": "أهلاً", "away_enabled": 1, "away_message": "مغلق",
		"day_sun": 1, "work_start": time(8), "work_end": time(17)}
	monkeypatch.setattr(auto, "now_datetime", lambda: datetime(2026, 10, 11, 20, 0))  # Sunday 20:00
	auto.on_incoming(_AD({"from": "966500000001", "content_type": "text"}))
	assert sent == ["أهلاً\n\nمغلق"]
	sent.clear()
	monkeypatch.setattr(auto, "now_datetime", lambda: datetime(2026, 10, 11, 10, 0))  # open
	fr.db.count = lambda *a, **k: 5
	auto.on_incoming(_AD({"from": "966500000001", "content_type": "text"}))
	assert sent == []
