"""IT08-01..IT08-16: transcription route media guards, upload limits, STT mapping.

Every case runs the real app factory, lifespan, `/api/transcriptions` route, the
real pure-ASGI upload guard and the real `OpenAICompatibleProvider` adapter over
per-role `httpx.MockTransport` handlers (IC §13.1: wire-failure cases inject the
actual HTTP transports). Only the external service is doubled; multipart parsing,
WAV validation, transcript trimming and error mapping are the shipped code.

The route never touches a Session: each case creates a real session first and
compares the session directory and file bytes before and after the upload.

Fixtures come from `tests/fixtures/audio/gen_wav.py` (stdlib `wave`/`struct`);
its docstring records every rate, duration and byte size.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Callable
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.errors import ERRORS
from app.main import create_app
from app.providers import OpenAICompatibleProvider
from tests.fixtures.audio import gen_wav
from tests.integration.support_api import raw_asgi
from tests.integration.test_it02_intake import start_session

STT_PATH = "/v1/audio/transcriptions"
TRANSCRIPT = "My order NS-000123 arrived broken and this is urgent."


async def no_sleep(delay: float) -> None:
    """Injected sleep: retry jitter never costs wall-clock time in tests."""


class RoleTransport:
    """A real httpx transport double that records each request it serves."""

    def __init__(self, role: str, responder: Callable[[httpx.Request, int], httpx.Response]):
        self.role = role
        self.responder = responder
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request, len(self.requests))


def unexpected(role: str):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise AssertionError(f"unexpected {role} wire call")

    return responder


def json_wire(payload: object, status: int = 200) -> httpx.Response:
    body = json.dumps(payload).encode("utf-8")
    return httpx.Response(
        status,
        stream=httpx.ByteStream(body),
        headers={"content-type": "application/json"},
    )


def stt_json(text: str):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire({"text": text})

    return responder


def stt_status(status: int):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire({"error": "unavailable"}, status=status)

    return responder


def stt_timeout():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.TimeoutException("held read timeout", request=request)

    return responder


class RecordingStream(httpx.AsyncByteStream):
    """A raw stream that records closure and how many bytes were handed out."""

    def __init__(self, chunks: list[bytes], gate: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate
        self.closed = False
        self.bytes_read = 0
        self.started = asyncio.Event()

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            self.bytes_read += len(chunk)
            self.started.set()
            yield chunk
            if index == 0 and self.gate is not None:
                await self.gate.wait()

    async def aclose(self) -> None:
        self.closed = True


def streamed_wire(
    chunks: list[bytes],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    gate: asyncio.Event | None = None,
) -> tuple[httpx.Response, RecordingStream]:
    stream = RecordingStream(chunks, gate)
    return httpx.Response(status, stream=stream, headers=headers or {}), stream


@asynccontextmanager
async def audio_client(settings, responders: dict[str, Callable], *, capacity: int = 4):
    """Run the real lifespan with the real adapter wired to mock transports."""

    profiles = {
        "llm": settings.llm,
        "rag": settings.rag,
        "stt": settings.stt,
        "tts": settings.tts,
    }
    recorders = {
        role: RoleTransport(role, responders.get(role, unexpected(role)))
        for role in ("llm", "rag", "stt", "tts")
    }
    provider = OpenAICompatibleProvider(
        profiles,
        transports={role: recorder.transport for role, recorder in recorders.items()},
        capacity=capacity,
        sleep=no_sleep,
    )
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client, recorders, app


def assert_error(response, *, status: int, code: str) -> dict:
    spec = ERRORS[code]
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == {
        "code": code,
        "message": spec.message,
        "retryable": spec.retryable,
    }
    assert set(body) == {"error", "request_id"}
    UUID(body["request_id"])
    return body


def snapshot(session_dir, session_id: str):
    """Session directory contents plus the exact bytes of the session document."""

    names = sorted(path.name for path in session_dir.iterdir())
    return names, (session_dir / f"{session_id}.json").read_bytes()


async def upload(client, payload: bytes, *, filename: str = "recording.wav"):
    return await client.post(
        "/api/transcriptions",
        files={"file": (filename, payload, "audio/wav")},
    )


# IT08-01 -------------------------------------------------------------------


async def case_it08_01(settings, session_dir):
    async with audio_client(settings, {"stt": stt_json(TRANSCRIPT)}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        payload = gen_wav.pcm_wav()
        assert len(payload) == 32044

        response = await upload(client, payload)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"text": TRANSCRIPT}
        assert len(rec["stt"].requests) == 1
        request = rec["stt"].requests[0]
        assert request.url.path == STT_PATH
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert request.headers["accept-encoding"] == "identity"
        body = request.content
        assert b'name="file"' in body
        assert b'filename="upload.wav"' in body
        assert payload in body
        assert snapshot(session_dir, session_id) == before
        assert rec["llm"].requests == [] and rec["tts"].requests == []


# IT08-02..IT08-07: guards reject before any paid call -----------------------


async def case_it08_02(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.silence_wav(), status=422, code="silent_audio")


async def case_it08_03(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.malformed_wav(), status=422, code="invalid_audio")


async def case_it08_04(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.truncated_wav(), status=422, code="invalid_audio")


async def case_it08_05(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.compressed_wav(), status=415, code="unsupported_audio")


async def case_it08_06(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.webm_bytes(), status=415, code="unsupported_audio")


async def case_it08_07(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.long_wav(), status=422, code="invalid_audio")


# IT08-08..IT08-10: upload caps ---------------------------------------------


async def case_it08_08(settings, session_dir):
    async with audio_client(settings, {}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        payload = gen_wav.oversize_file()
        assert len(payload) == gen_wav.MAX_FILE_BYTES + 1
        response = await upload(client, payload)
        assert_error(response, status=413, code="input_too_large")
        assert rec["stt"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_it08_09(settings, session_dir):
    body, boundary = gen_wav.oversized_multipart()
    assert len(body) == gen_wav.MAX_MULTIPART_BYTES + 1
    async with audio_client(settings, {}) as (client, rec, app):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        status, headers, raw = await raw_asgi(
            app,
            method="POST",
            path="/api/transcriptions",
            chunks=[body[index : index + 64 * 1024] for index in range(0, len(body), 64 * 1024)],
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
                "content-length": str(len(body)),
            },
        )
        assert status == 413
        assert json.loads(raw)["error"]["code"] == "input_too_large"
        assert rec["stt"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_it08_10(settings, session_dir):
    async with audio_client(settings, {}) as (client, rec, app):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        status, headers, raw = await raw_asgi(
            app,
            method="POST",
            path="/api/transcriptions",
            chunks=gen_wav.chunked_oversize(),
            headers={"content-type": "multipart/form-data; boundary=----orbio10chunked"},
        )
        assert status == 413
        assert json.loads(raw)["error"]["code"] == "input_too_large"
        assert rec["stt"].requests == []
        assert snapshot(session_dir, session_id) == before


async def assert_guard(settings, session_dir, payload: bytes, *, status: int, code: str):
    async with audio_client(settings, {}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        response = await upload(client, payload)
        assert_error(response, status=status, code=code)
        assert rec["stt"].requests == []
        assert rec["tts"].requests == []
        assert snapshot(session_dir, session_id) == before


# IT08-11..IT08-13: transcript post-conditions ------------------------------


async def assert_transcript_rejected(settings, session_dir, text: str, *, code: str):
    async with audio_client(settings, {"stt": stt_json(text)}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        response = await upload(client, gen_wav.pcm_wav())
        assert_error(response, status=422, code=code)
        assert len(rec["stt"].requests) == 1
        assert snapshot(session_dir, session_id) == before


async def case_it08_11(settings, session_dir):
    await assert_transcript_rejected(settings, session_dir, "", code="empty_transcription")


async def case_it08_12(settings, session_dir):
    await assert_transcript_rejected(
        settings, session_dir, "  \n\t  ", code="empty_transcription"
    )


async def case_it08_13(settings, session_dir):
    await assert_transcript_rejected(
        settings, session_dir, "x" * 4001, code="transcript_too_long"
    )


# IT08-14..IT08-15: STT service failures ------------------------------------


async def assert_stt_failure(settings, session_dir, responder, *, status: int, code: str):
    async with audio_client(settings, {"stt": responder}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        response = await upload(client, gen_wav.pcm_wav())
        assert_error(response, status=status, code=code)
        assert len(rec["stt"].requests) == 2
        assert rec["llm"].requests == [] and rec["tts"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_it08_14(settings, session_dir):
    await assert_stt_failure(
        settings,
        session_dir,
        stt_status(503),
        status=503,
        code="provider_unavailable",
    )


async def case_it08_15(settings, session_dir):
    await assert_stt_failure(
        settings, session_dir, stt_timeout(), status=504, code="provider_timeout"
    )


# IT08-16: cancellation -----------------------------------------------------


async def case_it08_16(settings, session_dir):
    gate = asyncio.Event()
    stream = RecordingStream([b'{"text": "partial', b'"}'], gate)

    def stt_gated(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(
                200, stream=stream, headers={"content-type": "application/json"}
            )
        return json_wire({"text": TRANSCRIPT})

    async with audio_client(settings, {"stt": stt_gated}, capacity=1) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)

        task = asyncio.create_task(upload(client, gen_wav.pcm_wav()))
        await asyncio.wait_for(stream.started.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.closed is True
        assert snapshot(session_dir, session_id) == before

        # The cancelled upload released its permit: a later valid STT succeeds
        # (capacity is 1, so a leaked permit would answer 429 provider_busy).
        response = await upload(client, gen_wav.pcm_wav())
        assert response.status_code == 200
        assert response.json() == {"text": TRANSCRIPT}
        assert len(rec["stt"].requests) == 2
        assert snapshot(session_dir, session_id) == before


CASES = {
    "IT08-01": case_it08_01,
    "IT08-02": case_it08_02,
    "IT08-03": case_it08_03,
    "IT08-04": case_it08_04,
    "IT08-05": case_it08_05,
    "IT08-06": case_it08_06,
    "IT08-07": case_it08_07,
    "IT08-08": case_it08_08,
    "IT08-09": case_it08_09,
    "IT08-10": case_it08_10,
    "IT08-11": case_it08_11,
    "IT08-12": case_it08_12,
    "IT08-13": case_it08_13,
    "IT08-14": case_it08_14,
    "IT08-15": case_it08_15,
    "IT08-16": case_it08_16,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_it08_transcription(case_id, settings, session_dir):
    await CASES[case_id](settings, session_dir)
