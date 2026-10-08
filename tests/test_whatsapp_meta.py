from datetime import datetime, timedelta

import pytest

from butler_whatsapp.meta import (
	CloudAPI, WhatsAppError, count_body_variables, document_message,
	normalize_msisdn, template_document_message, within_service_window,
)


@pytest.mark.parametrize("raw,expected", [
	("0505500842", "966505500842"),
	("+966 50 550 0842", "966505500842"),
	("00966505500842", "966505500842"),
	("505500842", "966505500842"),
	("966505500842", "966505500842"),
	("920012813", None),           # landline/unified number is not a mobile
	("", None), (None, None), ("abc", None),
	("+1 912 420 4922", "19124204922"),
])
def test_normalize(raw, expected):
	assert normalize_msisdn(raw) == expected


def test_window():
	now = datetime(2026, 10, 8, 16, 0)
	assert within_service_window(now - timedelta(hours=23, minutes=59), now)
	assert not within_service_window(now - timedelta(hours=24), now)
	assert not within_service_window(None, now)


def test_count_vars():
	assert count_body_variables("مرحبا {{1}}، عرض السعر {{2}} بقيمة {{3}}، {{1}}") == 3
	assert count_body_variables("no vars") == 0
	assert count_body_variables(None) == 0


def test_payloads():
	m = document_message("966500000000", "MID", "QTN-1.pdf", "hi")
	assert m["document"] == {"id": "MID", "filename": "QTN-1.pdf", "caption": "hi"}
	assert "caption" not in document_message("1", "M", "f.pdf")["document"]
	t = template_document_message("966500000000", "quote_doc", "ar", "MID", "QTN-1.pdf", ["A", 2])
	comps = t["template"]["components"]
	assert comps[0]["parameters"][0]["document"]["id"] == "MID"
	assert comps[1]["parameters"] == [{"type": "text", "text": "A"}, {"type": "text", "text": "2"}]
	assert len(template_document_message("1", "x", "ar", "M", "f.pdf")["template"]["components"]) == 1


class FakeResp:
	def __init__(self, status, data):
		self.status_code, self._d = status, data
	def json(self):
		return self._d


class FakeHTTP:
	def __init__(self, *resps):
		self.resps, self.calls = list(resps), []
	def post(self, url, **kw):
		self.calls.append((url, kw))
		return self.resps.pop(0)


def test_client_flow_and_errors():
	http = FakeHTTP(FakeResp(200, {"id": "MEDIA1"}), FakeResp(200, {"messages": [{"id": "wamid.X"}]}))
	api = CloudAPI("https://graph.facebook.com/", "v24.0", "PHONE", "TOKEN", session=http)
	assert api.upload_media(b"%PDF", "a.pdf") == "MEDIA1"
	assert http.calls[0][0] == "https://graph.facebook.com/v24.0/PHONE/media"
	assert http.calls[0][1]["headers"]["Authorization"] == "Bearer TOKEN"
	assert api.send({"x": 1}) == "wamid.X"
	bad = CloudAPI("https://g", "v1", "P", "T", session=FakeHTTP(FakeResp(400, {"error": {"message": "Re-engagement"}})))
	with pytest.raises(WhatsAppError, match="Re-engagement"):
		bad.send({})
