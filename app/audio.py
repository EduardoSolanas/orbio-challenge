"""Bounded PCM WAV validation and silent-recording detection (IC §7, §11).

No session state, no provider call: every failure here happens before a paid
transcription. A payload that is not a RIFF/WAVE container at all (WebM, MP3,
MP4 ...) is ``unsupported_audio``; a RIFF/WAVE file whose header or PCM geometry
is broken is ``invalid_audio``; a PCM file outside the accepted channels, sample
width or sample-rate window is ``unsupported_audio``; a valid PCM recording with
no sample reaching the fixed silence threshold is ``silent_audio``.
"""

from __future__ import annotations

import struct
import wave
from io import BytesIO

from app.errors import (
    INVALID_AUDIO,
    SILENT_AUDIO,
    UNSUPPORTED_AUDIO,
    app_error,
)

MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000
MAX_DURATION_SECONDS = 60
SUPPORTED_CHANNELS = (1, 2)
SUPPORTED_SAMPLE_WIDTH = 2
PCM_FORMAT_TAG = 1
SILENCE_PEAK_THRESHOLD = 128
FMT_CHUNK_SIZE = 16


def _fail(code: str):
    raise app_error(code)


def _fmt_fields(audio: bytes) -> tuple[int, int, int, int]:
    """Read the RIFF/WAVE ``fmt `` chunk: format tag, channels, rate, bits."""

    offset = 12
    while offset + 8 <= len(audio):
        chunk_id = audio[offset : offset + 4]
        size = struct.unpack("<I", audio[offset + 4 : offset + 8])[0]
        payload_start = offset + 8
        if chunk_id == b"fmt ":
            if size < FMT_CHUNK_SIZE or payload_start + FMT_CHUNK_SIZE > len(audio):
                _fail(INVALID_AUDIO)
            fmt, channels, rate, _byte_rate, _block_align, bits = struct.unpack(
                "<HHIIHH", audio[payload_start : payload_start + FMT_CHUNK_SIZE]
            )
            return fmt, channels, rate, bits
        offset = payload_start + size + (size % 2)
    _fail(INVALID_AUDIO)


def validate_wav(audio: bytes) -> None:
    """Validate PCM WAV geometry, duration, truncation and silence in order."""

    if len(audio) < 12 or audio[0:4] != b"RIFF" or audio[8:12] != b"WAVE":
        _fail(UNSUPPORTED_AUDIO)

    fmt, channels, rate, bits = _fmt_fields(audio)
    if fmt != PCM_FORMAT_TAG:
        _fail(UNSUPPORTED_AUDIO)
    if (
        channels not in SUPPORTED_CHANNELS
        or bits != SUPPORTED_SAMPLE_WIDTH * 8
        or not MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE
    ):
        _fail(UNSUPPORTED_AUDIO)

    try:
        with wave.open(BytesIO(audio)) as handle:
            frame_count = handle.getnframes()
            frame_bytes = handle.readframes(frame_count)
    except (EOFError, struct.error, wave.Error):
        _fail(INVALID_AUDIO)

    if frame_count <= 0:
        _fail(INVALID_AUDIO)
    if frame_count > MAX_DURATION_SECONDS * rate:
        _fail(INVALID_AUDIO)
    frame_size = channels * SUPPORTED_SAMPLE_WIDTH
    if len(frame_bytes) != frame_count * frame_size:
        _fail(INVALID_AUDIO)

    peak = 0
    for (sample,) in struct.iter_unpack("<h", frame_bytes):
        magnitude = -sample if sample < 0 else sample
        if magnitude > peak:
            peak = magnitude
    if peak < SILENCE_PEAK_THRESHOLD:
        _fail(SILENT_AUDIO)
