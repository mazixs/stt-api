"""Duration of a WebM/Opus payload, when it is a WebM/Opus at all.

The twin of `console.wavinfo`, and it exists for the same single reason: turning
measured processing time into a real-time factor. Browsers and Electron apps record
dictation with `MediaRecorder`, which emits WebM/Opus and offers the page no choice
of container, so this is the shape almost all real traffic arrives in.

The audio itself needs no help any more. Until 2.17.0 the engine refused these
uploads twice over — no Matroska demuxer, and an Opus parser that rejected the
multi-frame packets `MediaRecorder` always writes — so the console used to re-envelope
them into OGG before forwarding. Both halves are fixed upstream (gigastt#261, #263),
uploads now reach the engine byte for byte, and all that is left to do here is read
a number the engine reports only when the client asks for `verbose_json`, which
dictation clients do not.

No audio is decoded: a packet's length is written in its table-of-contents byte, so
summing the recording costs one walk over the block headers. Anything unparseable
returns None, exactly as `wav_duration_seconds` does for a non-WAV — an unknown
duration is reported as unknown rather than as zero.
"""

import struct

# EBML element IDs, marker bits included, as they appear on the wire.
_EBML_HEADER = b"\x1a\x45\xdf\xa3"
_SEGMENT = 0x18538067
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_CODEC_ID = 0x86
_CODEC_PRIVATE = 0x63A2
_CLUSTER = 0x1F43B675
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_SIMPLE_BLOCK = 0xA3

_MASTERS = (_SEGMENT, _TRACKS, _CLUSTER, _BLOCK_GROUP)

_SAMPLE_RATE = 48000  # Opus counts its samples at 48 kHz whatever the input rate was
_MAX_DEPTH = 8
_OPUS_HEAD_MIN = 12  # through the pre-skip field, which is all we read


class _Malformed(Exception):
    """Input is not something we can measure; the caller reports an unknown length."""


def webm_duration_seconds(data: bytes) -> float | None:
    """Seconds of Opus audio in a WebM payload, or None if it is not one."""
    if len(data) < 4 or data[:4] != _EBML_HEADER:
        return None
    state: dict = {"head": None, "track": None, "samples": 0}
    try:
        _walk(data, 0, len(data), state, 0)
        head = state["head"]
        if head is None or not state["samples"]:
            return None
        if len(head) < _OPUS_HEAD_MIN:
            raise _Malformed("OpusHead too short")
        # The encoder's warm-up samples are written into the stream but are not part
        # of the recording, so the player throws them away and so do we.
        pre_skip = struct.unpack_from("<H", head, 10)[0]
    except (_Malformed, IndexError, struct.error):
        return None
    seconds = (state["samples"] - pre_skip) / _SAMPLE_RATE
    return round(seconds, 3) if seconds > 0 else None


def _read_vint(data: bytes, pos: int, *, keep_marker: bool = False) -> tuple[int, int, bool]:
    """EBML variable-size integer: its value, the position after it, unknown-size flag."""
    first = data[pos]
    if not first:
        raise _Malformed("vint wider than 8 bytes")
    width = 9 - first.bit_length()
    end = pos + width
    if end > len(data):
        raise _Malformed("vint runs past end of input")
    if keep_marker:
        return int.from_bytes(data[pos:end], "big"), end, False
    value = int.from_bytes(data[pos:end], "big") & ((1 << (7 * width)) - 1)
    return value, end, value == (1 << (7 * width)) - 1


def _walk(data: bytes, pos: int, end: int, state: dict, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise _Malformed("nested too deeply")
    while pos < end:
        _id, pos, _ = _read_vint(data, pos, keep_marker=True)
        size, pos, unknown = _read_vint(data, pos)
        if _id in _MASTERS:
            # A live recording leaves Segment and every Cluster without a declared
            # size, because neither length is known while recording. Their children
            # are simply parsed at this level, which keeps recursion shallow however
            # many clusters the file has.
            if unknown:
                continue
            stop = pos + size
            if stop > end:
                raise _Malformed("master element runs past its parent")
            _walk(data, pos, stop, state, depth + 1)
            pos = stop
            continue
        if unknown:
            raise _Malformed("unknown size on a leaf element")
        stop = pos + size
        if stop > end:
            raise _Malformed("element runs past its parent")
        if _id == _TRACK_ENTRY:
            _track_entry(data, pos, stop, state)
        elif _id in (_SIMPLE_BLOCK, _BLOCK) and state["track"] is not None:
            packet = _block_frame(data, pos, stop, state["track"])
            if packet is not None:
                state["samples"] += _packet_samples(packet)
        pos = stop


def _track_entry(data: bytes, pos: int, end: int, state: dict) -> None:
    """Remember the first A_OPUS track: its number and its OpusHead."""
    number = codec = private = None
    while pos < end:
        _id, pos, _ = _read_vint(data, pos, keep_marker=True)
        size, pos, unknown = _read_vint(data, pos)
        if unknown or pos + size > end:
            raise _Malformed("bad TrackEntry child")
        body = data[pos : pos + size]
        if _id == _TRACK_NUMBER:
            number = int.from_bytes(body, "big")
        elif _id == _CODEC_ID:
            codec = body.rstrip(b"\x00")
        elif _id == _CODEC_PRIVATE:
            private = body
        pos += size
    if state["track"] is not None or codec != b"A_OPUS":
        return
    if number is None or private is None or not private.startswith(b"OpusHead"):
        raise _Malformed("Opus track without a usable OpusHead")
    state["track"] = number
    state["head"] = private


def _block_frame(data: bytes, pos: int, end: int, track: int) -> bytes | None:
    """The single Opus packet inside a SimpleBlock, or None for another track."""
    number, pos, _ = _read_vint(data, pos)
    if number != track:
        return None
    pos += 2  # relative timecode, int16
    if pos >= end:
        raise _Malformed("truncated block")
    if data[pos] & 0x06:
        # Lacing packs several frames into one block. MediaRecorder never does it for
        # audio, and counting one packet where there are several would understate the
        # recording, so we would rather admit we do not know.
        raise _Malformed("laced block")
    pos += 1
    if pos >= end:
        raise _Malformed("empty block")
    return data[pos:end]


def _packet_samples(packet: bytes) -> int:
    """Samples in one Opus packet, read from its table-of-contents byte (RFC 6716 §3.2).

    The frame count lives in the two low bits: one frame, two frames, two frames with
    an explicit first length, or — code 3, which is what `MediaRecorder` writes for its
    60 ms packets — a count in the byte that follows. The frame lengths themselves are
    never needed, only how many there are and how long one lasts.
    """
    toc = packet[0]
    code = toc & 0x03
    if code == 3:
        if len(packet) < 2:
            raise _Malformed("code 3 packet without a frame count")
        frames = packet[1] & 0x3F
        if not frames:
            raise _Malformed("code 3 packet with no frames")
    else:
        frames = 1 if code == 0 else 2
    return frames * _frame_samples(toc)


def _frame_samples(toc: int) -> int:
    config = toc >> 3
    if config < 12:  # SILK: 10, 20, 40, 60 ms
        return (480, 960, 1920, 2880)[config & 0x03]
    if config < 16:  # Hybrid: 10, 20 ms
        return (480, 960)[config & 0x01]
    return (120, 240, 480, 960)[config & 0x03]  # CELT: 2.5, 5, 10, 20 ms
