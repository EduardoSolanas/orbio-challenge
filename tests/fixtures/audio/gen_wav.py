"""Deterministic audio fixtures for the transcription route.

Every byte here is built with the standard library only (``wave``, ``struct``);
nothing is sampled from a third-party recording. The valid signal is a square
wave of exactly +/-1000 (period 40 frames), the silence fixture is all zeros.

Geometry of each fixture (rate / channels / sample width / duration / bytes):

- ``pcm_wav()``      16 kHz, mono, 16-bit PCM, 1.0 s -> 16000 frames,
                     32000 frame bytes, 32044 file bytes, peak +/-1000.
- ``silence_wav()``  same geometry as ``pcm_wav()`` but all-zero samples.
- ``long_wav()``     16 kHz, mono, 16-bit PCM, 61.0 s -> 976000 frames,
                     1952000 frame bytes (one second over the 60 s limit).
- ``truncated_wav()``16 kHz, mono, 16-bit PCM whose header declares 16000
                     frames while only 8000 frames (16000 bytes) are present.
- ``compressed_wav()`` RIFF/WAVE whose fmt chunk declares WAVE_FORMAT_IEEE_FLOAT
                     (tag 3) instead of PCM (tag 1); 16 kHz mono 16-bit, 16000 frames.
- ``webm_bytes()``   EBML magic plus a minimal WebM header; not a RIFF container.
- ``oversize_file()`` 10 MiB + 1 bytes of zero padding (over the file cap).
- ``oversized_multipart()`` a multipart body of exactly 10 MiB + 64 KiB + 1 bytes
                     whose ``file`` part is exactly 10 MiB, so only the total
                     multipart cap can reject it.

The transcription route accepts 10 MiB file bytes and 10 MiB + 64 KiB multipart
total (implementation contract section 7), so these fixtures sit exactly on and
just past each boundary.
"""

from __future__ import annotations

import struct
import wave
from functools import lru_cache
from io import BytesIO

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
PEAK = 1000
PERIOD_FRAMES = 40

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_FILE_BYTES + 64 * 1024

PCM_FORMAT = 1
IEEE_FLOAT_FORMAT = 3


def _frames(rate: int, seconds: float) -> int:
    return int(round(rate * seconds))


def _pcm_frames(*, frames: int, channels: int, peak: int) -> bytes:
    samples = bytearray()
    for index in range(frames):
        value = (
            0
            if peak == 0
            else (peak if (index // (PERIOD_FRAMES // 2)) % 2 == 0 else -peak)
        )
        samples += struct.pack("<h", value) * channels
    return bytes(samples)


def pcm_wav(
    *,
    rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    seconds: float = 1.0,
    peak: int = PEAK,
) -> bytes:
    """A valid finite PCM WAV: square wave of exactly +/-``peak``."""

    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(rate)
        writer.writeframes(
            _pcm_frames(frames=_frames(rate, seconds), channels=channels, peak=peak)
        )
    return buffer.getvalue()


def silence_wav() -> bytes:
    """A structurally valid PCM WAV whose samples are all zero."""

    return pcm_wav(peak=0)


def long_wav() -> bytes:
    """61 seconds of valid PCM, one second past the 60 s duration limit."""

    return pcm_wav(seconds=61.0)


def truncated_wav() -> bytes:
    """A WAV whose data chunk claims more frames than the file carries."""

    valid = pcm_wav()
    frame_bytes = _frames(SAMPLE_RATE, 1.0) * CHANNELS * SAMPLE_WIDTH
    # Keep the RIFF/data sizes intact (they declare the full duration) but drop
    # half of the sample bytes: readframes() returns fewer bytes than declared.
    return valid[: len(valid) - frame_bytes // 2]


def compressed_wav() -> bytes:
    """A well-formed RIFF/WAVE container whose fmt tag is IEEE float, not PCM."""

    frame_bytes = _frames(SAMPLE_RATE, 1.0) * CHANNELS * SAMPLE_WIDTH
    fmt = struct.pack(
        "<HHIIHH",
        IEEE_FLOAT_FORMAT,
        CHANNELS,
        SAMPLE_RATE,
        SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH,
        CHANNELS * SAMPLE_WIDTH,
        SAMPLE_WIDTH * 8,
    )
    data = struct.pack("<f", 0.5) * (frame_bytes // 4)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def malformed_wav() -> bytes:
    """RIFF/WAVE magic followed by a broken fmt chunk (no readable format)."""

    broken_fmt = b"fmt " + struct.pack("<I", 40) + b"\x00\x01\x02\x03"
    return b"RIFF" + struct.pack("<I", 4 + len(broken_fmt)) + b"WAVE" + broken_fmt


def webm_bytes() -> bytes:
    """A WebM/EBML header, not a RIFF/WAVE container."""

    return (
        b"\x1aE\xdf\xa3"  # EBML magic
        b"\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04\x42\xf3\x81\x08"
        b"\x42\x82\x88matroska\x42\x87\x81\x04\x42\x85\x81\x02"
        b"\x1f\x43\xb6\x75\x01\x00\x00\x00\x00\x00\x00\x00"
    )


@lru_cache(maxsize=1)
def oversize_file() -> bytes:
    """A 10 MiB + 1 byte payload: past the file cap, under the multipart cap."""

    return b"\x00" * (MAX_FILE_BYTES + 1)


def _multipart(boundary: str, file_bytes: bytes, pad: bytes) -> bytes:
    marker = boundary.encode("ascii")
    return b"".join(
        (
            b"--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            file_bytes,
            b"\r\n--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="pad"\r\n\r\n',
            pad,
            b"\r\n--" + marker + b"--\r\n",
        )
    )


def oversized_multipart() -> tuple[bytes, str]:
    """Total 10 MiB + 64 KiB + 1 with a ``file`` part of exactly 10 MiB."""

    boundary = "----orbio10oversize"
    file_bytes = b"\x00" * MAX_FILE_BYTES
    base = _multipart(boundary, file_bytes, b"")
    pad = b"x" * (MAX_MULTIPART_BYTES + 1 - len(base))
    body = _multipart(boundary, file_bytes, pad)
    assert len(body) == MAX_MULTIPART_BYTES + 1
    return body, boundary


def chunked_oversize() -> list[bytes]:
    """64 KiB ASGI chunks totalling 10 MiB + 64 KiB + 1 (no Content-Length)."""

    chunk = b"x" * (64 * 1024)
    total = MAX_MULTIPART_BYTES + 1
    return [chunk] * (total // len(chunk)) + [chunk[: total % len(chunk)]]
