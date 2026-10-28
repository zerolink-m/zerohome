"""Fragmented-MP4 framing shared by the stream manager and the ingest hub.

ffmpeg writes ftyp+moov (the init segment) followed by moof+mdat pairs. Pipe reads
split boxes at arbitrary offsets, so consumers must be fed whole boxes only (MSE
rejects anything else with "Invalid Top-Level Box")."""
from __future__ import annotations


# ffmpeg emits ftyp+moov within the first few hundred bytes; anything past this
# means the stream is not the fragmented MP4 we expect, so stop buffering.
MAX_INIT_SEGMENT_BYTES = 1024 * 1024
# A segment is a moof plus its mdat, so a pending buffer larger than this means
# the stream is malformed and framing it is pointless — forward it raw instead.
MAX_PENDING_BYTES = 8 * 1024 * 1024

_MP4_HEADER_SIZE = 8
_MP4_LARGE_SIZE = 1
_MP4_EXTENDS_TO_EOF = 0

_SAMPLE_IS_NON_SYNC = 0x00010000


def read_box(buf: bytearray | bytes, offset: int) -> tuple[bytes, int] | None:
    """Returns (box_type, total_box_size) for the box starting at `offset`, or
    None when that box is not fully present in the buffer (or its header is
    unusable).

    The full-box requirement is what makes framing correct: a box whose header
    has arrived but whose body has not must not be reported, otherwise a caller
    slicing `size` bytes would publish a truncated box and every following chunk
    would start mid-payload. Sizes are parsed rather than searched for, so a box
    type appearing inside an `mdat` payload can never be mistaken for a box."""
    if len(buf) - offset < _MP4_HEADER_SIZE:
        return None
    size = int.from_bytes(buf[offset:offset + 4], "big")
    box_type = bytes(buf[offset + 4:offset + 8])
    header_size = _MP4_HEADER_SIZE
    if size == _MP4_LARGE_SIZE:
        if len(buf) - offset < 16:
            return None
        size = int.from_bytes(buf[offset + 8:offset + 16], "big")
        header_size = 16
    elif size == _MP4_EXTENDS_TO_EOF or size < header_size:
        return None
    if len(buf) - offset < size:
        return None
    return box_type, size


def find_first_moof_offset(buf: bytearray | bytes) -> int | None:
    """Walks the top-level boxes in `buf` and returns the offset at which the
    first `moof` box starts, or None while that box is not in the buffer yet.

    Everything before that offset is the init segment (ftyp + moov) — ffmpeg may
    emit it across several reads, so it has to be accumulated rather than taken
    from a single chunk."""
    offset = 0
    while True:
        box = read_box(buf, offset)
        if box is None:
            return None
        box_type, size = box
        if box_type == b"moof":
            return offset
        offset += size


def _children(buf: bytes, start: int, end: int):
    offset = start
    while offset + _MP4_HEADER_SIZE <= end:
        box = read_box(buf[:end], offset)
        if box is None:
            return
        box_type, size = box
        header = 16 if int.from_bytes(buf[offset:offset + 4], "big") == _MP4_LARGE_SIZE else 8
        yield box_type, offset + header, offset + size
        offset += size


def fragment_starts_with_non_sync(fragment: bytes) -> bool:
    """True when the moof of `fragment` has a track whose FIRST sample is flagged
    non-sync (a video fragment that does not begin on a keyframe).

    With `frag_keyframe` ffmpeg cuts fragments on video keyframes, so this is normally
    False for every fragment; it exists so the hub never starts its GOP buffer in the
    middle of a GOP if the muxer is configured differently. Unparseable -> False
    (treated as a keyframe fragment)."""
    try:
        moof = read_box(fragment, 0)
        if moof is None or moof[0] != b"moof":
            return False
        for box_type, start, end in _children(fragment, 8, moof[1]):
            if box_type != b"traf":
                continue
            default_flags = None
            for ctype, cstart, cend in _children(fragment, start, end):
                if ctype == b"tfhd":
                    flags = int.from_bytes(fragment[cstart:cstart + 4], "big") & 0xFFFFFF
                    pos = cstart + 8  # version/flags + track_ID
                    if flags & 0x1:
                        pos += 8
                    if flags & 0x2:
                        pos += 4
                    if flags & 0x8:
                        pos += 4
                    if flags & 0x10:
                        pos += 4
                    if flags & 0x20:
                        default_flags = int.from_bytes(fragment[pos:pos + 4], "big")
                elif ctype == b"trun":
                    flags = int.from_bytes(fragment[cstart:cstart + 4], "big") & 0xFFFFFF
                    pos = cstart + 8  # version/flags + sample_count
                    if flags & 0x1:
                        pos += 4
                    first = None
                    if flags & 0x4:
                        first = int.from_bytes(fragment[pos:pos + 4], "big")
                        pos += 4
                    elif flags & 0x400:
                        sample_off = 0
                        if flags & 0x100:
                            sample_off += 4
                        if flags & 0x200:
                            sample_off += 4
                        first = int.from_bytes(fragment[pos + sample_off:pos + sample_off + 4], "big")
                    else:
                        first = default_flags
                    if first is not None and first & _SAMPLE_IS_NON_SYNC:
                        return True
                    break
        return False
    except Exception:
        return False


class Fmp4Splitter:
    """Incremental framer: feed raw pipe bytes, get the init segment once and whole
    media segments (moof + mdat) afterwards."""

    def __init__(self) -> None:
        self.init_segment: bytes | None = None  # ftyp + moov
        self.init_done: bool = False  # True once init was found OR gave up
        self._init_buffer = bytearray()
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Returns the media segments completed by `chunk`. Check `init_segment` /
        `init_done` afterwards for the init state."""
        if self.init_done:
            self._pending.extend(chunk)
            return self._drain()

        self._init_buffer.extend(chunk)
        moof_offset = find_first_moof_offset(self._init_buffer)
        if moof_offset is None:
            if len(self._init_buffer) > MAX_INIT_SEGMENT_BYTES:
                # No init segment in sight: give up instead of buffering the stream.
                self._init_buffer.clear()
                self.init_done = True
            return []

        self.init_segment = bytes(self._init_buffer[:moof_offset])
        self._pending.extend(self._init_buffer[moof_offset:])
        self._init_buffer.clear()
        self.init_done = True
        return self._drain()

    def _drain(self) -> list[bytes]:
        """Whole media segments (a `moof` plus the `mdat` after it)."""
        pending = self._pending
        out: list[bytes] = []
        while True:
            if len(pending) > MAX_PENDING_BYTES:
                out.append(bytes(pending))
                pending.clear()
                return out
            box = read_box(pending, 0)
            if box is None:
                return out
            box_type, size = box
            if box_type != b"moof":
                out.append(bytes(pending[:size]))
                del pending[:size]
                continue
            following = read_box(pending, size)
            if following is None:
                return out  # wait for the rest of the mdat
            following_type, following_size = following
            if following_type != b"mdat":
                out.append(bytes(pending[:size]))
                del pending[:size]
                continue
            total = size + following_size
            out.append(bytes(pending[:total]))
            del pending[:total]
