import pytest

from butler_whatsapp.meta import WhatsAppError, media_kind, media_message, phone_tail, text_message

MB = 1024 * 1024


@pytest.mark.parametrize("mime,size,kind", [
	("image/jpeg", MB, "image"),
	("image/png", 5 * MB, "image"),
	("image/png", 5 * MB + 1, "document"),  # too big for an image -> document
	("image/webp", MB, "document"),
	("image/gif", MB, "document"),
	("video/mp4", 10 * MB, "video"),
	("video/quicktime", 10 * MB, "document"),
	("audio/ogg", MB, "audio"),
	("audio/mpeg; charset=x", MB, "audio"),
	("application/pdf", 20 * MB, "document"),
	(None, 10, "document"),
])
def test_media_kind(mime, size, kind):
	assert media_kind(mime, size) == kind


def test_media_kind_too_big():
	with pytest.raises(WhatsAppError):
		media_kind("application/pdf", 101 * MB)


def test_media_message_shapes():
	assert media_message("966", "image", "ID", "a.jpg", "hi") == {
		"messaging_product": "whatsapp", "to": "966", "type": "image", "image": {"id": "ID", "caption": "hi"}}
	assert media_message("966", "document", "ID", "q.pdf", None)["document"] == {"id": "ID", "filename": "q.pdf"}
	# audio cannot carry a caption
	assert media_message("966", "audio", "ID", "v.ogg", "x")["audio"] == {"id": "ID"}


def test_text_message():
	assert text_message("966", "hello")["text"] == {"body": "hello", "preview_url": True}


@pytest.mark.parametrize("raw,tail", [
	("+966 50 550 0842", "505500842"),
	("0505500842", "505500842"),
	("966505500842", "505500842"),
	("050-550-0842", "505500842"),
	("12345", None),
	(None, None),
])
def test_phone_tail(raw, tail):
	assert phone_tail(raw) == tail


def test_template_message_and_render():
	from butler_whatsapp.meta import render_template, template_message, with_reply

	p = template_message("966", "offer_ar", "ar", ["علي", 5], {"type": "document", "document": {"id": "M"}})
	comps = p["template"]["components"]
	assert comps[0] == {"type": "header", "parameters": [{"type": "document", "document": {"id": "M"}}]}
	assert comps[1]["parameters"][1] == {"type": "text", "text": "5"}
	assert "components" not in template_message("966", "hi", "ar")["template"]
	assert render_template("مرحبا {{1}} و{{ 2 }}", ["أ", "ب"]) == "مرحبا أ وب"
	assert with_reply({"a": 1}, None) == {"a": 1}
	assert with_reply({"a": 1}, "wamid.X")["context"] == {"message_id": "wamid.X"}


def test_describe_location_and_contacts():
	from butler_whatsapp.meta import describe_contacts, describe_location

	assert describe_location({"latitude": 21.4, "longitude": 39.8, "name": "البيت", "address": "مكة"}).splitlines() == [
		"📍 البيت", "مكة", "https://maps.google.com/?q=21.4,39.8"]
	assert describe_contacts([{"name": {"formatted_name": "علي"}, "phones": [{"phone": "+966500"}]}]) == "👤 علي: +966500"
