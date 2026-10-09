"""WebM/Opus -> Ogg/Opus remux (pure Python, no ffmpeg).

Chrome and Edge record voice notes with MediaRecorder as audio/webm (Opus).
WhatsApp only accepts Opus inside Ogg, so the Opus packets are copied
unchanged from the WebM (Matroska) container into Ogg pages (RFC 7845).
No re-encoding happens, so quality and length stay identical.
"""

from __future__ import annotations

import os
import struct

# Matroska element ids we care about
_SEGMENT, _CLUSTER, _TRACKS, _TRACK_ENTRY = 0x18538067, 0x1F43B675, 0x1654AE6B, 0xAE
_BLOCK_GROUP, _BLOCK, _SIMPLE_BLOCK = 0xA0, 0xA1, 0xA3
_TRACK_NUMBER, _CODEC_ID, _CODEC_PRIVATE = 0xD7, 0x86, 0x63A2
_CHANNELS = 0x9F
_AUDIO = 0xE1
_MASTERS = {_SEGMENT, _CLUSTER, _TRACKS, _TRACK_ENTRY, _BLOCK_GROUP, _AUDIO}


class VoiceError(Exception):
	pass


def _read_id(data: bytes, pos: int) -> tuple[int, int]:
	first = data[pos]
	length = 1
	mask = 0x80
	while length <= 4 and not first & mask:
		mask >>= 1
		length += 1
	if length > 4:
		raise VoiceError("bad element id")
	return int.from_bytes(data[pos : pos + length], "big"), pos + length


def _read_size(data: bytes, pos: int) -> tuple[int | None, int]:
	first = data[pos]
	length = 1
	mask = 0x80
	while length <= 8 and not first & mask:
		mask >>= 1
		length += 1
	if length > 8:
		raise VoiceError("bad element size")
	value = first & (mask - 1)
	for b in data[pos + 1 : pos + length]:
		value = (value << 8) | b
	unknown = value == (1 << (7 * length)) - 1
	return (None if unknown else value), pos + length


def parse_webm_opus(data: bytes) -> tuple[bytes | None, int, list[bytes]]:
	"""Return (OpusHead from CodecPrivate or None, channels, opus packets)."""
	pos, end = 0, len(data)
	opus_track = None
	track_number = codec = private = None
	channels = 1
	head = None
	packets: list[bytes] = []

	while pos < end:
		try:
			eid, pos = _read_id(data, pos)
			size, pos = _read_size(data, pos)
		except (IndexError, VoiceError):
			break
		if eid in _MASTERS:
			if eid == _TRACK_ENTRY:
				track_number = codec = private = None
			continue  # descend: children follow inline (works with unknown sizes too)
		if size is None:
			raise VoiceError("unknown size on a leaf element")
		body = data[pos : pos + size]
		pos += size

		if eid == _TRACK_NUMBER:
			track_number = int.from_bytes(body, "big")
		elif eid == _CODEC_ID:
			codec = body.decode("ascii", "ignore")
		elif eid == _CODEC_PRIVATE:
			private = bytes(body)
		elif eid == _CHANNELS:
			channels = int.from_bytes(body, "big") or 1
		if codec == "A_OPUS" and track_number is not None and opus_track is None:
			opus_track = track_number
			head = private

		if eid in (_SIMPLE_BLOCK, _BLOCK):
			tnum, p = _read_size(body, 0)
			if opus_track is not None and tnum != opus_track:
				continue
			flags = body[p + 2]
			if flags & 0x06:
				raise VoiceError("laced blocks are not supported")
			packets.append(bytes(body[p + 3 :]))

	if opus_track is None:
		raise VoiceError("no Opus audio track found")
	return head, channels, packets


def packet_samples(packet: bytes) -> int:
	"""Number of 48 kHz samples in one Opus packet (RFC 6716, section 3.1)."""
	if not packet:
		return 0
	toc = packet[0]
	config = toc >> 3
	if config < 12:
		ms10 = (10, 20, 40, 60)[config % 4] * 10
	elif config < 16:
		ms10 = (10, 20)[config % 2] * 10
	else:
		ms10 = (25, 50, 100, 200)[config % 4]  # tenths of ms: 2.5, 5, 10, 20 ms
	code = toc & 0x03
	if code == 0:
		frames = 1
	elif code in (1, 2):
		frames = 2
	else:
		frames = (packet[1] & 0x3F) if len(packet) > 1 else 1
	return frames * ms10 * 48 // 10


# ------------------------------------------------------------------- ogg


def _crc_table():
	table = []
	for i in range(256):
		r = i << 24
		for _ in range(8):
			r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
		table.append(r & 0xFFFFFFFF)
	return table


_CRC = _crc_table()


def _ogg_crc(data: bytes) -> int:
	crc = 0
	for b in data:
		crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ b]
	return crc


def _page(packet: bytes, serial: int, seq: int, granule: int, flags: int) -> bytes:
	lacing = [255] * (len(packet) // 255) + [len(packet) % 255]
	if len(lacing) > 255:
		raise VoiceError("packet too large for one Ogg page")
	header = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, seq, 0, len(lacing)) + bytes(lacing)
	page = bytearray(header + packet)
	struct.pack_into("<I", page, 22, _ogg_crc(bytes(page)))
	return bytes(page)


def opus_head(channels: int = 1, pre_skip: int = 312, rate: int = 48000) -> bytes:
	return b"OpusHead" + struct.pack("<BBHIhB", 1, channels, pre_skip, rate, 0, 0)


def opus_tags(vendor: str = "butler_whatsapp") -> bytes:
	v = vendor.encode()
	return b"OpusTags" + struct.pack("<I", len(v)) + v + struct.pack("<I", 0)


def to_ogg(head: bytes | None, channels: int, packets: list[bytes], serial: int | None = None) -> bytes:
	if not packets:
		raise VoiceError("the recording is empty")
	if not head or not head.startswith(b"OpusHead"):
		head = opus_head(channels)
	serial = serial if serial is not None else struct.unpack("<I", os.urandom(4))[0]
	out = [_page(head, serial, 0, 0, 0x02), _page(opus_tags(), serial, 1, 0, 0)]
	granule = 0
	for i, packet in enumerate(packets):
		granule += packet_samples(packet)
		last = i == len(packets) - 1
		out.append(_page(packet, serial, i + 2, granule, 0x04 if last else 0))
	return b"".join(out)


def webm_to_ogg(data: bytes) -> bytes:
	head, channels, packets = parse_webm_opus(data)
	return to_ogg(head, channels, packets)
