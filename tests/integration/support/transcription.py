"""Provider doubles and wire helpers shared by the voice and provider tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Callable
from uuid import UUID

import httpx
from httpx import ASGITransport, AsyncClient

from gateway.errors import ERRORS
from gateway.main import create_app
from gateway.providers.client import OpenAICompatibleProvider


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
