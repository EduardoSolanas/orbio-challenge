"""Unit tests for bounded PCM WAV validation (IC §7, §11).

WAV containers are assembled byte-by-byte so each test controls exactly one
header, chunk or sample property and asserts the externally visible AppError
code (``unsupported_audio`` / ``invalid_audio`` / ``silent_audio``).
"""

from __future__ import annotations

import struct

import pytest

from backend.gateway.providers.audio import validate_wav
from backend.gateway.errors import AppError

LOUD = 1000


def chunk(chunk_id: bytes, payload: bytes, *, declared_size: int | None = None) -> bytes:
    size = len(payload) if declared_size is None else declared_size
    padding = b"\x00" if len(payload) % 2 else b""
    return chunk_id + struct.pack("<I", size) + payload + padding


def fmt_chunk(*, tag=1, channels=1, rate=16000, bits=16) -> bytes:
    block_align = channels * bits // 8
    return chunk(
        b"fmt ",
        struct.pack("<HHIIHH", tag, channels, rate, rate * block_align, block_align, bits),
    )


def data_chunk(samples: list[int], *, bits: int = 16) -> bytes:
    if bits == 16:
        return chunk(b"data", struct.pack(f"<{len(samples)}h", *samples))
    return chunk(b"data", bytes(sample & 0xFF for sample in samples))


def riff(*chunks: bytes, riff_tag: bytes = b"RIFF", wave_tag: bytes = b"WAVE") -> bytes:
    body = wave_tag + b"".join(chunks)
    return riff_tag + struct.pack("<I", len(body)) + body


def wav(samples=None, *, channels=1, rate=16000, bits=16, tag=1, before=()) -> bytes:
    samples = [LOUD] * (channels * 4) if samples is None else samples
    return riff(
        *before,
        fmt_chunk(tag=tag, channels=channels, rate=rate, bits=bits),
        data_chunk(samples, bits=bits),
    )


def code_of(audio: bytes) -> str | None:
    """Return the AppError code raised by validate_wav, or None when accepted."""

    try:
        validate_wav(audio)
    except AppError as error:
        return error.code
    return None


# --- accepted recordings ---------------------------------------------------


def test_valid_mono_recording_is_accepted():
    assert code_of(wav([LOUD] * 8)) is None


def test_valid_stereo_recording_is_accepted():
    assert code_of(wav([LOUD, -LOUD] * 8, channels=2)) is None


def test_non_fmt_chunks_before_fmt_are_skipped_including_odd_sized_and_padded_ones():
    before = (
        chunk(b"LIST", b"abcd"),
        chunk(b"junk", b"abc"),
        chunk(b"JUNK", b"abcdef"),
    )
    assert code_of(wav([LOUD] * 8, before=before)) is None


@pytest.mark.parametrize("rate", [8000, 48000])
def test_sample_rate_window_bounds_are_inclusive(rate):
    assert code_of(wav([LOUD] * 8, rate=rate)) is None


def test_a_single_loud_frame_is_a_valid_recording():
    assert code_of(wav([LOUD])) is None


def test_exactly_sixty_seconds_is_accepted():
    assert code_of(wav([LOUD] * (60 * 8000), rate=8000)) is None


# --- not a RIFF/WAVE container --------------------------------------------


def test_input_shorter_than_a_riff_header_is_unsupported():
    assert code_of(b"RIFF\x00\x00\x00\x00WAV") == "unsupported_audio"


def test_wrong_riff_tag_with_a_wave_tag_is_unsupported():
    assert code_of(wav([LOUD] * 8).replace(b"RIFF", b"RIFX", 1)) == "unsupported_audio"


def test_wave_tag_missing_behind_a_riff_tag_is_unsupported():
    audio = riff(fmt_chunk(), data_chunk([LOUD] * 8), wave_tag=b"AVI ")
    assert code_of(audio) == "unsupported_audio"


# --- RIFF/WAVE with broken chunk structure --------------------------------


def test_a_bare_twelve_byte_header_is_invalid_audio():
    assert code_of(riff()) == "invalid_audio"


def test_missing_fmt_chunk_is_invalid_audio():
    assert code_of(riff(data_chunk([LOUD] * 8))) == "invalid_audio"


def test_chunk_walk_ending_exactly_at_end_of_input_is_invalid_audio():
    assert code_of(riff(chunk(b"junk", b""))) == "invalid_audio"


def test_stray_bytes_after_the_last_chunk_are_invalid_audio():
    assert code_of(riff(chunk(b"junk", b"")) + b"abc") == "invalid_audio"


def test_fmt_chunk_declaring_fewer_than_sixteen_bytes_is_invalid_audio():
    short = chunk(b"fmt ", b"\x00" * 16, declared_size=15)
    assert code_of(riff(short, data_chunk([LOUD] * 8))) == "invalid_audio"


def test_fmt_payload_cut_short_by_end_of_input_is_invalid_audio():
    assert code_of(riff(chunk(b"fmt ", b"\x00" * 15))) == "invalid_audio"


def test_fmt_without_data_chunk_is_invalid_audio():
    assert code_of(riff(fmt_chunk())) == "invalid_audio"


def test_zero_frame_recording_is_invalid_audio():
    assert code_of(wav([])) == "invalid_audio"


def test_recording_longer_than_sixty_seconds_is_invalid_audio():
    assert code_of(wav([LOUD] * (60 * 8000 + 1), rate=8000)) == "invalid_audio"


def test_data_chunk_declaring_more_frames_than_present_is_invalid_audio():
    audio = riff(fmt_chunk(), chunk(b"data", struct.pack("<8h", *[LOUD] * 8), declared_size=64))
    assert code_of(audio) == "invalid_audio"


def test_chunk_size_above_two_gibibytes_is_treated_as_unsigned():
    # A junk chunk declaring 0xFFFFFFFF bytes must jump past the end of input;
    # it must not be read as -1 and reveal the fmt chunk placed right behind it.
    oversized = b"junk" + struct.pack("<I", 0xFFFFFFFF)
    audio = riff(oversized + fmt_chunk(channels=3))
    assert code_of(audio) == "invalid_audio"


# --- unsupported PCM geometry ---------------------------------------------


def test_fmt_chunk_ending_exactly_at_end_of_input_is_still_read():
    assert code_of(riff(fmt_chunk(channels=3))) == "unsupported_audio"


def test_non_pcm_format_tag_is_unsupported():
    assert code_of(wav([LOUD] * 8, tag=3)) == "unsupported_audio"


@pytest.mark.parametrize("channels", [0, 3])
def test_channel_counts_other_than_mono_or_stereo_are_unsupported(channels):
    assert code_of(wav([LOUD] * 12, channels=channels)) == "unsupported_audio"


def test_eight_bit_samples_are_unsupported():
    assert code_of(wav([LOUD] * 8, bits=8)) == "unsupported_audio"


@pytest.mark.parametrize("rate", [7999, 48001])
def test_sample_rates_outside_the_window_are_unsupported(rate):
    assert code_of(wav([LOUD] * 8, rate=rate)) == "unsupported_audio"


def test_every_geometry_violation_together_is_unsupported():
    assert code_of(wav([LOUD] * 12, channels=3, bits=8, rate=100)) == "unsupported_audio"


# --- silence detection -----------------------------------------------------


def test_all_zero_samples_are_silent():
    assert code_of(wav([0] * 16)) == "silent_audio"


def test_quiet_negative_samples_are_silent():
    assert code_of(wav([-1] * 16)) == "silent_audio"


def test_loud_negative_samples_are_not_silent():
    assert code_of(wav([-LOUD] * 16)) is None


def test_loud_positive_samples_are_not_silent():
    assert code_of(wav([LOUD] * 16)) is None


def test_peak_of_exactly_the_threshold_is_not_silent():
    assert code_of(wav([0, 128, 0, 0])) is None


def test_negative_peak_of_exactly_the_threshold_is_not_silent():
    assert code_of(wav([0, -128, 0, 0])) is None


def test_peak_just_below_the_threshold_is_silent():
    assert code_of(wav([127, -127, 0, 0])) == "silent_audio"


def test_a_single_loud_sample_among_silence_is_not_silent():
    assert code_of(wav([0] * 15 + [-LOUD])) is None
