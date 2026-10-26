"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic: lifecycle."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gateway.errors import AppError
from gateway.schemas import Extraction
from tests.unit.gateway.providers.provider_support import (
    EXTRACTION_PAYLOAD,
    build_provider,
    chat_envelope,
    extraction_context,
    extraction_responder,
    json_response,
    streamed_response,
)


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, entered: "asyncio.Event") -> None:
        self.entered = entered
        self.closed = False

    async def __aiter__(self):
        self.entered.set()
        await asyncio.Event().wait()
        yield b""

    async def aclose(self) -> None:
        self.closed = True


async def test_capacity_four_holds_permits_and_the_fifth_call_is_immediate_provider_busy():
    all_started = asyncio.Event()
    release = asyncio.Event()
    wire_calls = 0

    async def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        if wire_calls == 4:
            all_started.set()
        await release.wait()
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    provider, _ = build_provider({"llm": responder}, capacity=4)

    tasks = [
        asyncio.create_task(provider.extract(extraction_context())) for _ in range(4)
    ]
    await asyncio.wait_for(all_started.wait(), timeout=2)

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_busy"
    assert caught.value.status == 429
    assert caught.value.retryable is True
    assert wire_calls == 4

    release.set()
    results = await asyncio.gather(*tasks)
    assert all(isinstance(result, Extraction) for result in results)
    await provider.close()


async def test_cancellation_releases_the_permit_and_closes_the_response():
    entered = asyncio.Event()
    stream = BlockingStream(entered)
    wire_calls = 0

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        if wire_calls == 1:
            return httpx.Response(
                200, stream=stream, headers={"content-type": "audio/mpeg"}
            )
        return streamed_response(b"mp3", 200, {"content-type": "audio/mpeg"})

    provider, _ = build_provider({"tts": responder}, capacity=1)

    task = asyncio.create_task(provider.synthesize("hello"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed is True
    assert (await provider.synthesize("hello")).data == b"mp3"
    await provider.close()


async def test_permit_is_released_after_a_failed_operation():
    wire_calls = 0

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        if wire_calls == 1:
            return json_response({"error": "denied"}, status=401)
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    provider, _ = build_provider({"llm": responder}, capacity=1)

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())
    assert caught.value.code == "provider_configuration"

    assert isinstance(await provider.extract(extraction_context()), Extraction)
    await provider.close()


async def test_close_closes_all_four_clients_exactly_once():
    provider, _ = build_provider({"llm": extraction_responder()})
    counts: dict[str, int] = {}

    for role, client in provider._clients.items():
        original = client.aclose

        async def counted(original=original, role=role) -> None:
            counts[role] = counts.get(role, 0) + 1
            await original()

        client.aclose = counted

    await provider.close()
    await provider.close()

    assert counts == {"llm": 1, "rag": 1, "stt": 1, "tts": 1}
    assert all(client.is_closed for client in provider._clients.values())
