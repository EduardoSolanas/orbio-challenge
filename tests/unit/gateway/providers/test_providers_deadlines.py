"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic: deadlines."""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.errors import AppError
from gateway.providers.client import REPAIR_LINE
from tests.unit.gateway.providers.provider_support import (
    ChunkedStream,
    EXTRACTION_PAYLOAD,
    SequenceClock,
    SlowStream,
    build_provider,
    chat_envelope,
    extraction_context,
    padded_envelope,
    single_response,
)


async def test_multi_chunk_valid_json_over_the_cap_is_rejected():
    raw = padded_envelope(1024 * 1024 + 1).encode("utf-8")

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        stream = ChunkedStream([raw[:600000], raw[600000:]])
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2


async def test_text_asyncio_timeout_on_the_last_attempt_is_provider_timeout():
    body = json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))).encode("utf-8")

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200, stream=SlowStream(0.3, body), headers={"content-type": "application/json"}
        )

    provider, recorders = build_provider(
        {"llm": responder},
        clock=SequenceClock([1000.0, 1024.9, 1024.9, 1024.9]),
    )

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_timeout"
    assert caught.value.status == 504
    assert len(recorders["llm"].requests) == 2


async def test_tts_asyncio_timeout_is_provider_timeout_with_one_attempt():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200, stream=SlowStream(0.3, b"mp3"), headers={"content-type": "audio/mpeg"}
        )

    provider, recorders = build_provider(
        {"tts": responder}, clock=SequenceClock([1000.0, 1019.9])
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_timeout"
    assert len(recorders["tts"].requests) == 1


async def test_stt_retry_does_not_send_the_repair_line():
    provider, recorders = build_provider({"stt": single_response("not json")})

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["stt"].requests) == 2
    for request in recorders["stt"].requests:
        assert REPAIR_LINE.encode() not in request.content
        assert b'name="response_format"\r\n\r\njson\r\n' in request.content
