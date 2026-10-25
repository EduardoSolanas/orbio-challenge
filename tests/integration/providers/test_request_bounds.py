"""Pre-parse bounds, deadlines, cancellation, capacity and locks.

Cases 01-09 and 13-14 drive the real `OpenAICompatibleProvider` over per-role
`httpx.MockTransport` inside the real app lifespan (IC §13.2). Cases 10-12
invoke the real routes while a real turn holds the repository lock, so the 2 s
lock-acquisition limit and the 429 session_busy mapping are shipped code.

Streams are raw `httpx.AsyncByteStream` doubles that record closure and bytes
handed out, so "abort before retain/parse", "never decompress" and "response
closed" are observable rather than inferred. Retry jitter uses an injected sleep
and deadline arithmetic uses an injected clock; no case waits a production
deadline.
"""

from __future__ import annotations

import asyncio
import gzip
import json

import httpx
import pytest

from gateway.main import create_app
from gateway.providers.prompts import serialize_context
from gateway.providers.client import REPAIR_LINE, SUCCESS_JSON_LIMIT
from gateway.schemas import Extraction
from tests.integration.support.adapter import (
    EXTRACTION_PAYLOAD,
    SleepRecorder,
    adapter_app,
    assert_app_error,
    chat_envelope,
    env,
    extraction_context,
    padded_envelope,
    settings_from,
)
from tests.integration.support.api import (
    ScriptedProvider,
    make_extraction,
    read_session,
)
from tests.integration.support.bounds import blocked_stream_responder
from tests.integration.support.intake import PART_TEXT, start_session, turn_body
from tests.integration.support.transcription import (
    RecordingStream,
    assert_error,
    json_wire,
    snapshot,
    streamed_wire,
)


ERROR_BODY_LIMIT = 64 * 1024


def chat_valid(payload: object = EXTRACTION_PAYLOAD):
    content = json.dumps(payload)

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire(chat_envelope(content))

    return responder


def streaming_status(status: int, body: bytes):
    """A non-2xx response whose raw stream records reads and closure."""

    streams: list[RecordingStream] = []

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        response, stream = streamed_wire([body], status=status)
        streams.append(stream)
        return response

    responder.streams = streams
    return responder


class TimeoutStream(httpx.AsyncByteStream):
    """A slow provider read: one chunk then a transport read timeout."""

    def __init__(self, first: bytes) -> None:
        self.first = first
        self.bytes_read = 0
        self.closed = False

    async def __aiter__(self):
        self.bytes_read += len(self.first)
        yield self.first
        raise httpx.ReadTimeout("provider read exceeded the attempt budget")

    async def aclose(self) -> None:
        self.closed = True


def slow_read_responder():
    streams: list[TimeoutStream] = []

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        stream = TimeoutStream(b'{"choices":[')
        streams.append(stream)
        return httpx.Response(
            200, stream=stream, headers={"content-type": "application/json"}
        )

    responder.streams = streams
    return responder


class BlockingProvider(ScriptedProvider):
    """Scripted AIProvider whose extract can hold the real turn lock until released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.block = False

    async def extract(self, context):
        self.calls.append(("extract", context))
        self.entered.set()
        if self.block:
            await self.gate.wait()
        if not self.extractions:
            raise AssertionError("unexpected extract() call")
        return self.extractions.popleft()


# successful text json exactly1mib valid envelope padding..successful json content encoding gzip despite identity: provider body bounds --------------------------------------


async def case_successful_text_json_exactly1mib_valid_envelope_padding(session_dir):
    settings = settings_from(env(session_dir))
    body = padded_envelope(SUCCESS_JSON_LIMIT)
    assert len(body) == 1024 * 1024

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"content-type": "application/json"},
        )

    async with adapter_app(settings, {"llm": responder}) as harness:
        extraction = await harness.provider.extract(extraction_context())
        assert extraction == Extraction.model_validate(EXTRACTION_PAYLOAD)
        assert len(harness.recorders["llm"].requests) == 1


async def case_successful_text_json1mib_1(session_dir):
    settings = settings_from(env(session_dir))
    body = padded_envelope(SUCCESS_JSON_LIMIT + 1)
    assert len(body) == SUCCESS_JSON_LIMIT + 1

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"content-type": "application/json"},
        )

    async with adapter_app(settings, {"llm": responder}) as harness:
        session_id = await start_session(harness.client)
        before = snapshot(session_dir, session_id)
        response = await harness.client.post(
            f"/api/sessions/{session_id}/turns", json=turn_body(PART_TEXT, 0)
        )
        assert_error(response, status=502, code="invalid_provider_response")
        assert len(harness.recorders["llm"].requests) == 2
        assert snapshot(session_dir, session_id) == before


async def case_http401_error_body_exactly64kib(session_dir, caplog):
    settings = settings_from(env(session_dir))
    secret = "sk-error-body-secret-sentinel"
    body = secret.encode() + b"x" * (ERROR_BODY_LIMIT - len(secret))
    assert len(body) == ERROR_BODY_LIMIT
    responder = streaming_status(401, body)

    async with adapter_app(settings, {"llm": responder}) as harness:
        with caplog.at_level("DEBUG"):
            with pytest.raises(Exception) as excinfo:
                await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_configuration")
        assert len(harness.recorders["llm"].requests) == 1
        stream = responder.streams[0]
        assert stream.bytes_read == 0
        assert stream.closed is True
        assert secret not in str(excinfo.value)
        assert secret not in caplog.text


async def case_http401_body64kib_1(session_dir, caplog):
    settings = settings_from(env(session_dir))
    secret = "sk-oversize-error-secret-sentinel"
    body = secret.encode() + b"y" * (ERROR_BODY_LIMIT + 1 - len(secret))
    assert len(body) == ERROR_BODY_LIMIT + 1
    responder = streaming_status(401, body)

    async with adapter_app(settings, {"llm": responder}) as harness:
        with caplog.at_level("DEBUG"):
            with pytest.raises(Exception) as excinfo:
                await harness.provider.extract(extraction_context())
        # Status-derived nonretryable configuration mapping survives the
        # oversized body: it is never 502 and never retried.
        assert_app_error(excinfo.value, status=503, code="provider_configuration")
        assert len(harness.recorders["llm"].requests) == 1
        stream = responder.streams[0]
        assert stream.bytes_read == 0
        assert stream.closed is True
        assert secret not in str(excinfo.value)
        assert secret not in caplog.text


async def case_http429_body64kib_1(session_dir, caplog):
    settings = settings_from(env(session_dir))
    secret = "sk-retryable-error-secret-sentinel"
    body = secret.encode() + b"z" * (ERROR_BODY_LIMIT + 1 - len(secret))
    responder = streaming_status(429, body)

    async with adapter_app(settings, {"llm": responder}) as harness:
        with caplog.at_level("DEBUG"):
            with pytest.raises(Exception) as excinfo:
                await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(harness.recorders["llm"].requests) == 2
        assert len(responder.streams) == 2
        for stream in responder.streams:
            assert stream.bytes_read == 0
            assert stream.closed is True
        assert secret not in str(excinfo.value)
        assert secret not in caplog.text


async def case_successful_json_content_encoding_gzip_despite_identity(session_dir):
    settings = settings_from(env(session_dir))
    plain = json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))).encode()
    compressed = gzip.compress(plain)
    streams: list[RecordingStream] = []

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        response, stream = streamed_wire(
            [compressed],
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )
        streams.append(stream)
        return response

    async with adapter_app(settings, {"llm": responder}) as harness:
        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=502, code="invalid_provider_response")
        assert len(harness.recorders["llm"].requests) == 2
        assert len(streams) == 2
        # Rejected before any decompression or retention.
        assert all(stream.bytes_read == 0 for stream in streams)
        assert all(stream.closed for stream in streams)


# slow streaming json read..adapter global capacity occupied: slow read, cancellation and capacity ----------------------


async def case_slow_streaming_json_read(session_dir):
    settings = settings_from(env(session_dir))
    responder = slow_read_responder()
    async with adapter_app(settings, {"llm": responder}) as harness:
        session_id = await start_session(harness.client)
        before = snapshot(session_dir, session_id)
        response = await harness.client.post(
            f"/api/sessions/{session_id}/turns", json=turn_body(PART_TEXT, 0)
        )
        # The whole read and cleanup live inside the attempt/deadline budget.
        assert_error(response, status=504, code="provider_timeout")
        assert len(harness.recorders["llm"].requests) == 2
        assert len(responder.streams) == 2
        assert all(stream.closed for stream in responder.streams)
        assert all(stream.bytes_read == len(b'{"choices":[') for stream in responder.streams)
        assert snapshot(session_dir, session_id) == before


async def case_canceled_provider_request_before_result(session_dir):
    settings = settings_from(env(session_dir))
    gate = asyncio.Event()
    started = asyncio.Event()
    responder = blocked_stream_responder(gate, started)

    async with adapter_app(settings, {"llm": responder}, capacity=1) as harness:
        task = asyncio.create_task(harness.provider.extract(extraction_context()))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        stream = responder.streams[0]
        assert stream.closed is True

        # The permit was released: a later operation can acquire capacity.
        extraction = await asyncio.wait_for(
            harness.provider.extract(extraction_context()), timeout=5
        )
        assert extraction == Extraction.model_validate(EXTRACTION_PAYLOAD)
        assert len(harness.recorders["llm"].requests) == 2


async def case_adapter_global_capacity_occupied(session_dir):
    settings = settings_from(env(session_dir))
    gate = asyncio.Event()
    started = asyncio.Event()
    responder = blocked_stream_responder(gate, started)

    async with adapter_app(settings, {"llm": responder}, capacity=1) as harness:
        held = asyncio.create_task(harness.provider.extract(extraction_context()))
        await asyncio.wait_for(started.wait(), timeout=5)

        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=429, code="provider_busy")
        # Rejected immediately: no queued or extra wire call.
        assert len(harness.recorders["llm"].requests) == 1

        gate.set()
        await asyncio.wait_for(held, timeout=5)


# get session while turn lock held beyond2s..second turn lock acquisition beyond2s: 2 s repository lock through the real routes ---------------


async def case_get_session_while_turn_lock_held_beyond2s(session_dir):
    settings = settings_from(env(session_dir))
    provider = BlockingProvider()
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session_id = await start_session(client)
            provider.script_extraction(make_extraction())
            before = snapshot(session_dir, session_id)
            provider.block = True

            turn = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/turns",
                    json=turn_body(PART_TEXT, 0),
                )
            )
            await asyncio.wait_for(provider.entered.wait(), timeout=5)

            response = await asyncio.wait_for(
                client.get(f"/api/sessions/{session_id}"), timeout=10
            )
            assert_error(response, status=429, code="session_busy")
            # No snapshot, no partial commit while the lock is held.
            assert snapshot(session_dir, session_id) == before

            provider.gate.set()
            completed = await asyncio.wait_for(turn, timeout=10)
            assert completed.status_code == 200, completed.text
            assert read_session(session_dir, session_id)["revision"] == 1


async def case_speech_message_lookup_while_same_lock_held_beyond2s(session_dir):
    settings = settings_from(env(session_dir))
    provider = BlockingProvider()
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session_id = await start_session(client)
            provider.script_extraction(make_extraction())
            first = await client.post(
                f"/api/sessions/{session_id}/turns",
                json=turn_body(PART_TEXT, 0),
            )
            assert first.status_code == 200, first.text
            message_id = first.json()["message_id"]
            before = snapshot(session_dir, session_id)

            provider.script_extraction(make_extraction())
            provider.entered.clear()
            provider.block = True
            turn = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/turns",
                    json=turn_body("It is a damaged item.", 1),
                )
            )
            await asyncio.wait_for(provider.entered.wait(), timeout=5)

            response = await asyncio.wait_for(
                client.post(
                    f"/api/sessions/{session_id}/speech",
                    json={"message_id": message_id},
                ),
                timeout=10,
            )
            assert_error(response, status=429, code="session_busy")
            assert "synthesize" not in provider.methods()
            assert snapshot(session_dir, session_id) == before

            provider.gate.set()
            completed = await asyncio.wait_for(turn, timeout=10)
            assert completed.status_code == 200, completed.text


async def case_second_turn_lock_acquisition_beyond2s(session_dir):
    settings = settings_from(env(session_dir))
    provider = BlockingProvider()
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session_id = await start_session(client)
            provider.script_extraction(make_extraction())
            before = snapshot(session_dir, session_id)
            provider.block = True

            first_turn = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/turns",
                    json=turn_body(PART_TEXT, 0),
                )
            )
            await asyncio.wait_for(provider.entered.wait(), timeout=5)

            provider.script_extraction(make_extraction())
            second = await asyncio.wait_for(
                client.post(
                    f"/api/sessions/{session_id}/turns",
                    json=turn_body("It is a damaged item.", 0),
                ),
                timeout=10,
            )
            assert_error(second, status=429, code="session_busy")
            # No second extraction and no queued turn behind the held lock.
            assert provider.methods().count("extract") == 1
            assert snapshot(session_dir, session_id) == before

            provider.gate.set()
            completed = await asyncio.wait_for(first_turn, timeout=10)
            assert completed.status_code == 200, completed.text


# transient error then valid response..malformed schema then valid repair response: retry and repair budgets ----------------------------------


async def case_transient_error_then_valid_response(session_dir):
    settings = settings_from(env(session_dir))
    sleep = SleepRecorder()

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return json_wire({"error": "transient"}, status=500)
        return json_wire(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    async with adapter_app(settings, {"llm": responder}, sleep=sleep) as harness:
        extraction = await harness.provider.extract(extraction_context())
        assert extraction == Extraction.model_validate(EXTRACTION_PAYLOAD)
        assert len(harness.recorders["llm"].requests) == 2
        assert len(sleep.calls) == 1
        assert 0.25 <= sleep.calls[0] <= 0.5


async def case_malformed_schema_then_valid_repair_response(session_dir):
    settings = settings_from(env(session_dir))

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return json_wire(chat_envelope(json.dumps({"intent": "provide_details"})))
        return json_wire(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    async with adapter_app(settings, {"llm": responder}) as harness:
        extraction = await harness.provider.extract(extraction_context())
        assert extraction == Extraction.model_validate(EXTRACTION_PAYLOAD)

        requests = harness.recorders["llm"].requests
        assert len(requests) == 2  # exactly one repair, never a third attempt
        first, second = requests
        first_payload = json.loads(first.content)
        second_payload = json.loads(second.content)
        assert second_payload["messages"][1]["content"] == first_payload["messages"][1][
            "content"
        ]
        assert second_payload["messages"][1]["content"] == serialize_context(
            extraction_context()
        )
        assert second_payload["messages"][0]["content"] == (
            first_payload["messages"][0]["content"] + "\n" + REPAIR_LINE
        )
        assert REPAIR_LINE not in first_payload["messages"][0]["content"]


CASES = {
    "successful_text_json_exactly1mib_valid_envelope_padding": case_successful_text_json_exactly1mib_valid_envelope_padding,
    "successful_text_json1mib_1": case_successful_text_json1mib_1,
    "http401_error_body_exactly64kib": case_http401_error_body_exactly64kib,
    "http401_body64kib_1": case_http401_body64kib_1,
    "http429_body64kib_1": case_http429_body64kib_1,
    "successful_json_content_encoding_gzip_despite_identity": case_successful_json_content_encoding_gzip_despite_identity,
    "slow_streaming_json_read": case_slow_streaming_json_read,
    "canceled_provider_request_before_result": case_canceled_provider_request_before_result,
    "adapter_global_capacity_occupied": case_adapter_global_capacity_occupied,
    "get_session_while_turn_lock_held_beyond2s": case_get_session_while_turn_lock_held_beyond2s,
    "speech_message_lookup_while_same_lock_held_beyond2s": case_speech_message_lookup_while_same_lock_held_beyond2s,
    "second_turn_lock_acquisition_beyond2s": case_second_turn_lock_acquisition_beyond2s,
    "transient_error_then_valid_response": case_transient_error_then_valid_response,
    "malformed_schema_then_valid_repair_response": case_malformed_schema_then_valid_repair_response,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_bounds(case_id, session_dir, caplog):
    case = CASES[case_id]
    if "caplog" in case.__code__.co_varnames[: case.__code__.co_argcount]:
        await case(session_dir, caplog)
    else:
        await case(session_dir)
