import pytest
from conftest import OPUS_HEAD, opus_code3_packet, webm_opus_bytes, wav_bytes

from console.webminfo import webm_duration_seconds

PRE_SKIP = 312  # what conftest writes into the OpusHead
FRAME = 960  # samples in one 20 ms frame at 48 kHz


def expected(frames: int) -> float:
    return (frames * FRAME - PRE_SKIP) / 48000


def approx(seconds: float) -> object:
    return pytest.approx(seconds, abs=0.001)


def test_mediarecorder_shape_is_measured():
    """60 ms packets of three 20 ms frames — what every browser writes."""
    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)] * 50)
    assert webm_duration_seconds(webm) == approx(expected(150))


def test_encoder_warmup_is_not_part_of_the_recording():
    one = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)])
    assert webm_duration_seconds(one) == approx(expected(3))


def test_every_packet_framing_counts_its_own_frames():
    """RFC 6716 §3.2: one frame, two frames, two with an explicit length, or a count."""
    single = bytes((0xFC,)) + b"m" * 30
    two_equal = bytes((0xFD,)) + b"m" * 60
    two_explicit = bytes((0xFE, 30)) + b"m" * 60
    webm = webm_opus_bytes([single, two_equal, two_explicit])
    assert webm_duration_seconds(webm) == approx(expected(1 + 2 + 2))


def test_frame_length_comes_from_the_configuration_not_the_payload():
    """Config 0 is a 10 ms SILK frame, so the same byte count lasts half as long."""
    webm = webm_opus_bytes([bytes((0x00,)) + b"m" * 30] * 2)
    assert webm_duration_seconds(webm) == approx((2 * 480 - PRE_SKIP) / 48000)


def test_blocks_of_another_track_are_not_counted():
    webm = webm_opus_bytes(
        [opus_code3_packet([b"m" * 30] * 3)] * 3, decoy=(2, b"V_VP8")
    )
    assert webm_duration_seconds(webm) == approx(expected(9))


def test_a_recording_of_nothing_has_no_duration():
    assert webm_duration_seconds(webm_opus_bytes([])) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x1a\x45",
        wav_bytes(seconds=1),
        b"OggS" + b"\x00" * 100,
        b"\x00" * 512,
    ],
    ids=["empty", "two-bytes", "wav", "ogg", "zeros"],
)
def test_anything_that_is_not_webm_is_left_to_the_engine(payload):
    assert webm_duration_seconds(payload) is None


def test_a_truncated_recording_is_not_guessed_at():
    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)] * 20)
    assert webm_duration_seconds(webm[: len(webm) // 2]) is None


def test_laced_blocks_are_admitted_as_unknown_rather_than_undercounted():
    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)], lacing=0x02)
    assert webm_duration_seconds(webm) is None


def test_a_track_that_is_not_opus_is_not_measured():
    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)], codec=b"A_VORBIS")
    assert webm_duration_seconds(webm) is None


def test_an_opus_track_without_a_head_is_not_measured():
    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)], codec_private=None)
    assert webm_duration_seconds(webm) is None


def test_a_truncated_opus_head_is_not_measured():
    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)], codec_private=OPUS_HEAD[:10])
    assert webm_duration_seconds(webm) is None


def test_a_code_three_packet_claiming_no_frames_is_rejected():
    webm = webm_opus_bytes([bytes((0xFF, 0x00)) + b"m" * 30])
    assert webm_duration_seconds(webm) is None


def test_corrupted_recordings_never_raise():
    """Whatever a half-written upload contains, the answer is a number or None."""
    webm = bytearray(webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)] * 4))
    for index in range(0, len(webm), 7):
        mangled = bytearray(webm)
        mangled[index] ^= 0xFF
        result = webm_duration_seconds(bytes(mangled))
        assert result is None or result > 0
