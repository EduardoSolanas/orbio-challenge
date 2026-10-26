"""Shared helpers for the split test_providers modules."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import httpx

from gateway.config import ServiceProfile, SpeechProfile, TextProfile
from gateway.providers.client import OpenAICompatibleProvider
from gateway.schemas import ExtractionContext, Slots
from tests.support.provider_builders import SOURCE, knowledge_context, unexpected  # noqa: F401  (re-exported to the unit tests)

ROLES = ("llm", "rag", "stt", "tts")

LLM_PROFILE = TextProfile(
    base_url="http://llm.test/v1",
    model="llm-model",
    auth_mode="none",
    api_key="",
    output_mode="json_schema",
)

RAG_PROFILE = TextProfile(
    base_url="http://rag.test/v1",
    model="rag-model",
    auth_mode="none",
    api_key="",
    output_mode="json_schema",
)

STT_PROFILE = ServiceProfile(
    base_url="http://stt.test/v1", model="stt-model", auth_mode="none", api_key=""
)

TTS_PROFILE = SpeechProfile(
    base_url="http://tts.test/v1",
    model="tts-model",
    auth_mode="none",
    api_key="",
    voice="alloy",
)

EXTRACTION_PAYLOAD = {
    "intent": "provide_details",
    "has_question": False,
    "candidates": {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": "high",
    },
    "explicit_corrections": [],
    "ambiguous_fields": [],
    "detected_language": "en",
    "requested_language": None,
    "sentiment": "neutral",
    "frustration": "none",
}

ANSWER_PAYLOAD = {
    "answerable": True,
    "answer": "You can request a return within 30 calendar days.",
    "cited_ids": ["faq-returns"],
}


def extraction_context(
    text: str = "My order NS-000123 arrived broken.",
) -> ExtractionContext:
    return ExtractionContext(
        slots=Slots(order_number=None, category=None, description=None, urgency=None),
        pending_clarifications={},
        recent_messages=[],
        current_message=text,
        status="collecting",
        intake_started=False,
        frustration_active=False,
    )


def chat_envelope(
    content: object, finish_reason: object = "stop", **message_extra: object
):
    message = {"content": content, **message_extra}
    return {"choices": [{"finish_reason": finish_reason, "message": message}]}


def streamed_response(
    body: bytes, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    """A real streaming response: not pre-consumed, so aiter_raw works."""

    return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers or {})


def json_response(
    payload: object, status: int = 200, headers: dict[str, str] | None = None
):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return streamed_response(body.encode("utf-8"), status, headers)


class RoleTransport:
    """MockTransport that records each request and delegates to a responder."""

    def __init__(
        self, role: str, responder: Callable[[httpx.Request, int], httpx.Response]
    ):
        self.role = role
        self.responder = responder
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request, len(self.requests))


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def build_provider(
    responders: dict[str, Callable[[httpx.Request, int], httpx.Response]],
    *,
    profiles: dict[str, ServiceProfile] | None = None,
    sleep: Callable | None = None,
    clock: Callable | None = None,
    capacity: int = 4,
):
    role_profiles = profiles or {
        "llm": LLM_PROFILE,
        "rag": RAG_PROFILE,
        "stt": STT_PROFILE,
        "tts": TTS_PROFILE,
    }
    recorders = {
        role: RoleTransport(role, responders.get(role, unexpected(role)))
        for role in ROLES
    }
    provider = OpenAICompatibleProvider(
        role_profiles,
        transports={role: recorders[role].transport for role in ROLES},
        capacity=capacity,
        sleep=sleep if sleep is not None else SleepRecorder(),
        clock=clock if clock is not None else Clock(),
    )
    return provider, recorders


def extraction_responder(payload: dict | None = None):
    body = chat_envelope(json.dumps(payload or EXTRACTION_PAYLOAD))

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response(body)

    return responder


def padded_envelope(target_bytes: int) -> str:
    base = chat_envelope(json.dumps(EXTRACTION_PAYLOAD))
    base["pad"] = ""
    pad = target_bytes - len(json.dumps(base).encode("utf-8"))
    assert pad >= 0
    base["pad"] = "x" * pad
    raw = json.dumps(base)
    assert len(raw.encode("utf-8")) == target_bytes
    return raw


def single_response(
    body: str | bytes, status: int = 200, headers: dict[str, str] | None = None
):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        content = body.encode("utf-8") if isinstance(body, str) else body
        return streamed_response(content, status, headers)

    return responder


def audio_responder(
    body: bytes, headers: dict[str, str] | None = None, status: int = 200
):
    resolved = {"content-type": "audio/mpeg"} if headers is None else headers

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(body, status, resolved)

    return responder


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class SequenceClock:
    """Clock returning a fixed script of values; repeats the last one."""

    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.index = 0

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, delay: float, body: bytes) -> None:
        self.delay = delay
        self.body = body

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield self.body
