"""Duration of a WAV payload, when it is a WAV at all.

Used only to turn measured processing time into a real-time factor. Any other
container (mp3, m4a, ogg) returns None — the engine decodes those, and the console
deliberately has no audio dependencies.
"""

import struct


def wav_duration_seconds(data: bytes) -> float | None:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    offset = 12
    sample_rate = channels = bits = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack_from("<I", data, offset + 4)
        body = offset + 8
        if chunk_id == b"fmt " and size >= 16:
            _fmt, channels, sample_rate, _byte_rate, _align, bits = struct.unpack_from(
                "<HHIIHH", data, body
            )
        elif chunk_id == b"data":
            bytes_per_frame = max(channels * max(bits, 8) // 8, 1)
            if sample_rate <= 0:
                return None
            available = min(size, len(data) - body)
            return round(available / bytes_per_frame / sample_rate, 3)
        offset = body + size + (size % 2)
    return None
