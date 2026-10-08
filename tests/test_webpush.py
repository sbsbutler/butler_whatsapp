"""Web Push crypto: interoperates with the reference http_ece implementation (if available)
and round-trips through our own RFC 8291 decryption."""

import json
import os
import sys

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from butler_whatsapp import webpush as wp


def _browser_keys():
	ua = ec.generate_private_key(ec.SECP256R1())
	pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
	auth = os.urandom(16)
	return ua, wp.b64u_encode(pub), wp.b64u_encode(auth), pub, auth


def _decrypt(body, ua, ua_pub, auth):
	# Independent decoder written straight from RFC 8291 / 8188 (receiver side).
	salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
	as_pub = body[21:21 + idlen]
	ct = body[21 + idlen:]
	assert rs == 4096 and idlen == 65
	shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
	h = lambda s, k, i, n: HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=i).derive(k)
	ikm = h(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
	pt = AESGCM(h(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(
		h(salt, ikm, b"Content-Encoding: nonce\x00", 12), ct, None)
	assert pt.endswith(b"\x02")
	return pt[:-1]


def test_round_trip():
	ua, ua_b64, auth_b64, ua_pub, auth = _browser_keys()
	msg = json.dumps({"title": "واتساب", "body": "مرحبا"}, ensure_ascii=False).encode()
	assert _decrypt(wp.encrypt(msg, ua_b64, auth_b64), ua, ua_pub, auth) == msg


def test_matches_reference_http_ece():
	ref = os.environ.get("HTTP_ECE_SRC", "/tmp/ref/http_ece-1.2.1")
	if not os.path.isdir(ref):
		pytest.skip("reference http_ece source not available")
	sys.path.insert(0, ref)
	import http_ece

	ua, ua_b64, auth_b64, ua_pub, auth = _browser_keys()
	as_priv = ec.generate_private_key(ec.SECP256R1())
	salt = os.urandom(16)
	msg = b"When I grow up, I want to be a watermelon"
	ours = wp.encrypt(msg, ua_b64, auth_b64, salt=salt, as_private=as_priv)
	theirs = http_ece.encrypt(msg, salt=salt, private_key=as_priv, dh=ua_pub, auth_secret=auth,
		version="aes128gcm", rs=4096)
	assert ours == theirs
	assert http_ece.decrypt(ours, private_key=ua, auth_secret=auth, version="aes128gcm") == msg


def test_vapid_header_is_valid_es256_jwt():
	priv, pub = wp.generate_vapid_keys()
	assert wp.vapid_public_key(priv) == pub and len(wp.b64u_decode(pub)) == 65
	header = wp.vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", priv, "mailto:a@b.c", now=1000)
	t = header.split("t=")[1].split(",")[0]
	k = header.split("k=")[1]
	assert k == pub
	h, c, s = t.split(".")
	claims = json.loads(wp.b64u_decode(c))
	assert claims == {"aud": "https://fcm.googleapis.com", "exp": 1000 + 43200, "sub": "mailto:a@b.c"}
	sig = wp.b64u_decode(s)
	pubkey = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.b64u_decode(pub))
	pubkey.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
		f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))


class _Resp:
	def __init__(self, code): self.status_code, self.text = code, ""


class _HTTP:
	def __init__(self, code): self.code, self.calls = code, []
	def post(self, url, **kw):
		self.calls.append((url, kw)); return _Resp(self.code)


def test_send_headers_and_gone():
	priv, _ = wp.generate_vapid_keys()
	_, ua_b64, auth_b64, _, _ = _browser_keys()
	sub = {"endpoint": "https://web.push.apple.com/QGx", "keys": {"p256dh": ua_b64, "auth": auth_b64}}
	http = _HTTP(201)
	assert wp.send(sub, {"title": "x"}, priv, "mailto:a@b.c", session=http) == 201
	hdr = http.calls[0][1]["headers"]
	assert hdr["Content-Encoding"] == "aes128gcm" and hdr["Urgency"] == "high"
	assert hdr["Authorization"].startswith("vapid t=")
	with pytest.raises(wp.Gone):
		wp.send(sub, {"title": "x"}, priv, "mailto:a@b.c", session=_HTTP(410))


@pytest.mark.parametrize("url,ok", [
	("https://fcm.googleapis.com/fcm/send/abc", True),
	("https://web.push.apple.com/QGx", True),
	("https://api.push.apple.com/3/device/x", True),
	("https://updates.push.services.mozilla.com/wpush/v2/x", True),
	("https://wns2-db5p.notify.windows.com/w/?token=x", True),
	("http://fcm.googleapis.com/fcm/send/abc", False),
	("https://evil.com/fcm.googleapis.com", False),
	("https://fcm.googleapis.com.evil.com/x", False),
	("https://127.0.0.1/x", False),
	("https://butler.com.sa/api/method/x", False),
	("", False),
])
def test_push_endpoint_allowlist(url, ok):
	assert wp.is_push_endpoint(url) is ok
