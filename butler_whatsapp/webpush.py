"""Minimal Web Push sender (RFC 8291 aes128gcm + RFC 8292 VAPID).

Pure Python on top of `cryptography` and `requests` (both already shipped with
Frappe) so no extra dependency is needed on Frappe Cloud. Works with Chrome /
Android (FCM endpoints), Safari / iOS 16.4+ home-screen apps and Firefox.
"""

from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
_TIMEOUT = 15

# Browser push services. Subscriptions pointing anywhere else are rejected so a
# user cannot make the server POST to arbitrary (e.g. internal) URLs.
PUSH_HOST_SUFFIXES = (
	"fcm.googleapis.com",  # Chrome, Edge (Android/desktop), Samsung Internet
	".push.apple.com",  # Safari / iOS home-screen apps
	".push.services.mozilla.com",  # Firefox
	".notify.windows.com",  # legacy Edge
)


def is_push_endpoint(endpoint: str) -> bool:
	parsed = urlparse(endpoint or "")
	host = (parsed.hostname or "").lower()
	return parsed.scheme == "https" and any(
		host == suffix.lstrip(".") or host.endswith(suffix) for suffix in PUSH_HOST_SUFFIXES
	)


def b64u_encode(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
	return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
	return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _private_from_raw(raw_b64: str) -> ec.EllipticCurvePrivateKey:
	return ec.derive_private_key(int.from_bytes(b64u_decode(raw_b64), "big"), ec.SECP256R1())


def generate_vapid_keys() -> tuple[str, str]:
	"""Return (private_key, public_key) as base64url; public is the browser's applicationServerKey."""
	key = ec.generate_private_key(ec.SECP256R1())
	raw = key.private_numbers().private_value.to_bytes(32, "big")
	return b64u_encode(raw), b64u_encode(_public_bytes(key.public_key()))


def vapid_public_key(private_b64: str) -> str:
	return b64u_encode(_public_bytes(_private_from_raw(private_b64).public_key()))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
	return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
	payload: bytes,
	ua_public_b64: str,
	auth_b64: str,
	salt: bytes | None = None,
	as_private: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
	"""Encrypt a push message body (RFC 8291, single aes128gcm record)."""
	ua_public = b64u_decode(ua_public_b64)
	auth = b64u_decode(auth_b64)
	salt = salt or os.urandom(16)
	as_private = as_private or ec.generate_private_key(ec.SECP256R1())
	as_public = _public_bytes(as_private.public_key())

	ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
	shared = as_private.exchange(ec.ECDH(), ua_key)
	ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
	cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
	nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

	if len(payload) + 1 + 16 + 86 > RECORD_SIZE:
		raise ValueError("payload too large for a single push record")
	ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02 = last-record delimiter
	header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
	return header + ciphertext


def vapid_authorization(endpoint: str, private_b64: str, subject: str, now: int | None = None) -> str:
	"""`Authorization: vapid t=<jwt>, k=<public key>` header value (ES256 JWT)."""
	key = _private_from_raw(private_b64)
	parsed = urlparse(endpoint)
	claims = {
		"aud": f"{parsed.scheme}://{parsed.netloc}",
		"exp": int(now or time.time()) + 12 * 3600,
		"sub": subject,
	}
	header = {"typ": "JWT", "alg": "ES256"}
	signing_input = (
		b64u_encode(json.dumps(header, separators=(",", ":")).encode())
		+ "."
		+ b64u_encode(json.dumps(claims, separators=(",", ":")).encode())
	)
	r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
	signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
	return f"vapid t={signing_input}.{b64u_encode(signature)}, k={vapid_public_key(private_b64)}"


class Gone(Exception):
	"""The subscription no longer exists (HTTP 404/410): delete it."""


def send(subscription: dict, data: dict, private_b64: str, subject: str, ttl: int = 86400, session=None) -> int:
	"""Send one notification. `subscription` = {endpoint, keys: {p256dh, auth}}."""
	body = encrypt(
		json.dumps(data, ensure_ascii=False).encode(),
		subscription["keys"]["p256dh"],
		subscription["keys"]["auth"],
	)
	resp = (session or requests).post(
		subscription["endpoint"],
		data=body,
		headers={
			"Authorization": vapid_authorization(subscription["endpoint"], private_b64, subject),
			"Content-Encoding": "aes128gcm",
			"Content-Type": "application/octet-stream",
			"TTL": str(ttl),
			"Urgency": "high",
		},
		timeout=_TIMEOUT,
	)
	if resp.status_code in (404, 410):
		raise Gone(resp.status_code)
	if resp.status_code >= 400:
		raise RuntimeError(f"push service HTTP {resp.status_code}: {resp.text[:200]}")
	return resp.status_code
