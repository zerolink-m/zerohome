"""Builds pseudo fragmented MP4 (valid box structure, fake payloads) for tests."""
from __future__ import annotations

import struct

SYNC = 0x02000000  # sample_depends_on=2, sync sample
NON_SYNC = 0x01010000  # depends on others + non-sync flag


def box(box_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def init_segment(extra: int = 64) -> bytes:
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    moov = box(b"moov", box(b"mvhd", b"\0" * 100) + b"\0" * extra)
    return ftyp + moov


def fragment(seq: int, key: bool = True, size: int = 1000) -> bytes:
    """moof(mfhd, traf(tfhd, trun)) + mdat. Payload starts with b'F' + seq (4 bytes)."""
    mfhd = box(b"mfhd", struct.pack(">II", 0, seq))
    tfhd = box(b"tfhd", struct.pack(">II", 0x020000, 1))
    flags = SYNC if key else NON_SYNC
    # version/flags (data-offset + first-sample-flags), sample_count, data_offset, first_sample_flags
    trun = box(b"trun", struct.pack(">IIiI", 0x000005, 1, 0, flags))
    moof = box(b"moof", mfhd + box(b"traf", tfhd + trun))
    payload = b"F" + struct.pack(">I", seq)
    payload += b"\0" * max(0, size - len(payload))
    return moof + box(b"mdat", payload)


def seq_of(fragment_bytes: bytes) -> int:
    """Reads back the sequence number stored in the mdat payload."""
    idx = fragment_bytes.index(b"mdat") + 4
    assert fragment_bytes[idx:idx + 1] == b"F"
    return struct.unpack(">I", fragment_bytes[idx + 1:idx + 5])[0]
