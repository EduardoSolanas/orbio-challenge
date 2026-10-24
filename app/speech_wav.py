"""Provider-side speech helpers: split reply text and assemble WAV chunks.

Long replies are split at the TTS profile's input limit; each returned WAV chunk
is decoded and the PCM frames are joined under one new RIFF/WAVE header (PC §4).
Uploaded customer audio is validated separately in ``app.audio``.
"""

from __future__ import annotations

import struct
import wave
from io import BytesIO
from typing import Sequence

TTS_LIMIT = 5 * 1024 * 1024
WAV_MIN_RATE = 8000
WAV_MAX_RATE = 48000
WAV_MAX_CHANNELS = 2
WAV_SAMPLE_WIDTH = 2
WAVE_FORMAT_PCM = 1
STREAMING_CHUNK_SIZE = 0xFFFFFFFF

PcmChunk = tuple[int, int, int, bytes]
"""``(channels, sample width in bytes, sample rate, frame bytes)``."""


def split_tts_text(text: str, limit: int) -> list[str]:
    """Partition ``text`` into consecutive slices of at most ``limit`` code points."""

    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _parse_pcm_wav(data: bytes) -> PcmChunk:
    """Read one RIFF/WAVE payload, treating ``0xFFFFFFFF`` sizes as unspecified.

    Streaming encoders (Groq Orpheus) emit ``0xFFFFFFFF`` for the RIFF and data
    sizes because the length is unknown up front. The bytes actually received
    are the frame payload; finite sizes remain authoritative and are rejected
    when they exceed the buffer.
    """

    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a decodable PCM WAV")
    offset = 12
    geometry: tuple[int, int, int] | None = None
    # A RIFF chunk header is at least 8 bytes, so this bounds iteration even if
    # a malformed chunk size would otherwise move the cursor backwards.
    for _ in range(len(data) // 8):
        if offset + 8 > len(data):
            break
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = offset + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + size > len(data):
                raise ValueError("not a decodable PCM WAV")
            format_tag, channels, rate, _, _, bits = struct.unpack_from(
                "<HHLLHH", data, body
            )
            if format_tag != WAVE_FORMAT_PCM or channels == 0 or bits == 0:
                raise ValueError("not a decodable PCM WAV")
            geometry = (channels, (bits + 7) // 8, rate)
        elif chunk_id == b"data":
            if geometry is None:
                raise ValueError("not a decodable PCM WAV")
            available = len(data) - body
            if size == STREAMING_CHUNK_SIZE:
                payload = data[body:]
            elif size > available:
                raise ValueError("empty or truncated WAV frames")
            else:
                payload = data[body : body + size]
            channels, width, rate = geometry
            if not payload or len(payload) % (channels * width):
                raise ValueError("empty or truncated WAV frames")
            return channels, width, rate, payload
        elif size == STREAMING_CHUNK_SIZE:
            raise ValueError("not a decodable PCM WAV")
        offset = body + size + (size & 1)
    raise ValueError("not a decodable PCM WAV")


def decode_pcm_wav(data: bytes) -> PcmChunk:
    """Decode one provider WAV chunk into ``(channels, sample width, rate, frames)``.

    Accepts complete uncompressed PCM WAVs, including streaming WAVs whose RIFF
    and data sizes are ``0xFFFFFFFF``. Raises ``ValueError`` unless the payload
    is complete, frame-aligned and within the accepted geometry window.
    """

    channels, width, rate, frames = _parse_pcm_wav(data)
    if not 1 <= channels <= WAV_MAX_CHANNELS or width != WAV_SAMPLE_WIDTH:
        raise ValueError("unsupported WAV geometry")
    if not WAV_MIN_RATE <= rate <= WAV_MAX_RATE:
        raise ValueError("unsupported WAV sample rate")
    return channels, width, rate, frames


def assemble_wav(parts: Sequence[PcmChunk]) -> bytes:
    """Join PCM chunks under one new RIFF/WAVE header, preserving frame order."""

    if not parts:
        raise ValueError("no WAV chunks to assemble")
    channels, width, rate = parts[0][0], parts[0][1], parts[0][2]
    frames = bytearray()
    for part_channels, part_width, part_rate, payload in parts:
        if (part_channels, part_width, part_rate) != (channels, width, rate):
            raise ValueError("mismatched WAV geometry between chunks")
        frames += payload
    if len(frames) > TTS_LIMIT:
        raise ValueError("assembled WAV exceeds the response cap")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))
    return buffer.getvalue()
