"""Read the geometry and frames of a WAV payload."""

from __future__ import annotations

import wave
from io import BytesIO


def wav_geometry(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(BytesIO(data)) as reader:
        channels, width, rate = (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
        )
        count = reader.getnframes()
        return channels, width, rate, reader.readframes(count)
