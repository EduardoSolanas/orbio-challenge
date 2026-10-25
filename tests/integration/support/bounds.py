"""Blocking stream responder shared by the bounds and startup tests."""

from __future__ import annotations

import asyncio
import json

import httpx

from tests.integration.support.adapter import EXTRACTION_PAYLOAD, chat_envelope
from tests.integration.support.transcription import (
    RecordingStream,
    json_wire,
    streamed_wire,
)


def blocked_stream_responder(gate: asyncio.Event, started: asyncio.Event):
    """First call streams one chunk then waits forever; later calls succeed."""

    streams: list[RecordingStream] = []

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            response, stream = streamed_wire([b'{"choices":['], gate=gate)
            streams.append(stream)
            started.set()
            return response
        return json_wire(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    responder.streams = streams
    return responder
