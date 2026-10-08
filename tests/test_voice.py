"""WebM/Opus -> Ogg/Opus remux on a synthetic Chrome-like WebM (unknown sizes)."""

import struct

import pytest

from butler_whatsapp import voice


def _vint_size(n: int) -> bytes:
	for length in range(1, 9):
		if n < (1 << (7 * length)) - 1:
			return (n | (1 << (7 * length))).to_bytes(length, "big")
	raise ValueError


def _el(eid: int, body: bytes) -> bytes:
	return eid.to_bytes((eid.bit_length() + 7) // 8, "big") + _vint_size(len(body)) + body


UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def _webm(packets, head=None):
	head = head if head is not None else voice.opus_head(1)
	track = _el(0xAE, _el(0xD7, b"\x01") + _el(0x86, b"A_OPUS") + _el(0x63A2, head))
	blocks = b"".join(_el(0xA3, b"\x81" + struct.pack(">h", i * 20) + b"\x80" + p) for i, p in enumerate(packets))
	cluster = (0x1F43B675).to_bytes(4, "big") + UNKNOWN + _el(0xE7, b"\x00") + blocks
	segment = (0x18538067).to_bytes(4, "big") + UNKNOWN + _el(0x1654AE6B, track) + cluster
	ebml = _el(0x1A45DFA3, _el(0x4282, b"webm"))
	return ebml + segment


def _pages(ogg: bytes):
	pos, pages = 0, []
	while pos < len(ogg):
		assert ogg[pos : pos + 4] == b"OggS"
		flags, granule, serial, seq, crc, nseg = struct.unpack("<BqIIIB", ogg[pos + 5 : pos + 27])
		lacing = ogg[pos + 27 : pos + 27 + nseg]
		size = 27 + nseg + sum(lacing)
		page = bytearray(ogg[pos : pos + size])
		struct.pack_into("<I", page, 22, 0)
		assert voice._ogg_crc(bytes(page)) == crc
		pages.append((flags, granule, seq, bytes(page[27 + nseg :])))
		pos += size
	return pages


def test_remux_unknown_size_webm():
	# TOC 0xF8 = config 31 (CELT 20 ms), one frame -> 960 samples each
	packets = [bytes([0xF8]) + bytes([i]) * 40 for i in range(5)]
	ogg = voice.webm_to_ogg(_webm(packets))
	pages = _pages(ogg)
	assert pages[0][3].startswith(b"OpusHead") and pages[0][0] == 0x02
	assert pages[1][3].startswith(b"OpusTags")
	audio = pages[2:]
	assert [p[3] for p in audio] == packets
	assert [p[1] for p in audio] == [960 * (i + 1) for i in range(5)]
	assert audio[-1][0] == 0x04  # end of stream
	assert [p[2] for p in pages] == list(range(7))


def test_missing_codec_private_builds_head():
	ogg = voice.webm_to_ogg(_webm([bytes([0xF8, 1])], head=b""))
	assert _pages(ogg)[0][3] == voice.opus_head(1)


@pytest.mark.parametrize("toc,extra,samples", [
	(0xF8, b"", 960),      # CELT 20ms x1
	(0xF9, b"", 1920),     # CELT 20ms x2 (code 1)
	(0xFB, b"\x03", 2880), # code 3, 3 frames
	(0x08, b"", 960),      # SILK 20ms
	(0x18, b"", 2880),     # SILK 60ms
	(0x80, b"", 120),      # CELT 2.5ms
])
def test_packet_samples(toc, extra, samples):
	assert voice.packet_samples(bytes([toc]) + extra + b"x") == samples


def test_not_opus():
	with pytest.raises(voice.VoiceError):
		voice.webm_to_ogg(_el(0x1A45DFA3, b""))
