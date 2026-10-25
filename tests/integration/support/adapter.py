"""Provider-adapter app factory, environment and envelope helpers."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable

import httpx

from app.config import Settings
from app.errors import ERRORS
from app.main import create_app
from app.providers import OpenAICompatibleProvider
from app.schemas import ExtractionContext, Slots
from tests.integration.support.transcription import (
    RoleTransport,
    json_wire,
    no_sleep,
    unexpected,
)


ROLES = ("llm", "rag", "stt", "tts")

DEFAULT_ENV = {
    "AI_PROVIDER": "openai_compatible",
    "LLM_BASE_URL": "http://llm.test/v1",
    "LLM_MODEL": "llm-model",
    "LLM_AUTH_MODE": "bearer",
    "LLM_API_KEY": "llm-token",
    "LLM_OUTPUT_MODE": "json_schema",
    "RAG_BASE_URL": "http://rag.test/v1",
    "RAG_MODEL": "rag-model",
    "RAG_AUTH_MODE": "bearer",
    "RAG_API_KEY": "rag-token",
    "RAG_OUTPUT_MODE": "json_schema",
    "STT_BASE_URL": "http://stt.test/v1",
    "STT_MODEL": "stt-model",
    "STT_AUTH_MODE": "bearer",
    "STT_API_KEY": "stt-token",
    "TTS_BASE_URL": "http://tts.test/v1",
    "TTS_MODEL": "tts-model",
    "TTS_AUTH_MODE": "bearer",
    "TTS_API_KEY": "tts-token",
    "TTS_VOICE": "voice-1",
    "PROVIDER_MAX_CONCURRENCY": "4",
    "ORDER_API_BASE_URL": "http://order-api.test",
}

ENV_KEYS = sorted(
    set(DEFAULT_ENV)
    | {
        "SESSION_DIR",
        "OPENAI_API_KEY",
        "PROVIDER_API_KEY",
        "LLM_OUTPUT_MODE",
        "RAG_OUTPUT_MODE",
        "TTS_VOICE",
    }
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


def env(session_dir, **overrides) -> dict[str, str]:
    """A complete isolated environment; ``None`` removes a key entirely."""

    values = dict(DEFAULT_ENV)
    values["SESSION_DIR"] = str(session_dir)
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def settings_from(values: dict[str, str]) -> Settings:
    return Settings.from_env(values)


def apply_env(monkeypatch, values: dict[str, str]) -> None:
    """Apply an isolated environment to the process for a real ``create_app()``."""

    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def extraction_context() -> ExtractionContext:
    return ExtractionContext(
        slots=Slots(order_number=None, category=None, description=None, urgency=None),
        pending_clarifications={},
        recent_messages=[],
        current_message="My order NS-000123 arrived broken.",
        status="collecting",
        intake_started=False,
        frustration_active=False,
    )


def chat_envelope(
    content: object,
    finish_reason: object = "stop",
    message_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"content": content}
    message.update(message_extra or {})
    return {"choices": [{"finish_reason": finish_reason, "message": message}]}


def chat_valid(payload: object = EXTRACTION_PAYLOAD, **kwargs) -> Callable:
    body = json.dumps(chat_envelope(json.dumps(payload), **kwargs))

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire(json.loads(body))

    return responder


def padded_envelope(target_bytes: int) -> bytes:
    """A valid chat envelope padded to exactly ``target_bytes`` raw bytes."""

    envelope = chat_envelope(json.dumps(EXTRACTION_PAYLOAD))
    envelope["pad"] = ""
    pad = target_bytes - len(json.dumps(envelope).encode("utf-8"))
    assert pad >= 0
    envelope["pad"] = "x" * pad
    raw = json.dumps(envelope).encode("utf-8")
    assert len(raw) == target_bytes
    return raw


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


@dataclass
class Harness:
    provider: OpenAICompatibleProvider
    recorders: dict[str, RoleTransport]
    app: object
    client: httpx.AsyncClient


@asynccontextmanager
async def adapter_app(
    settings: Settings,
    responders: dict[str, Callable],
    *,
    capacity: int = 4,
    sleep: Callable | None = None,
    clock: Callable | None = None,
    raise_app_exceptions: bool = True,
):
    """Real lifespan around the real adapter with one controlled transport per role."""

    profiles = {
        "llm": settings.llm,
        "rag": settings.rag,
        "stt": settings.stt,
        "tts": settings.tts,
    }
    recorders = {
        role: RoleTransport(role, responders.get(role, unexpected(role)))
        for role in ROLES
    }
    provider = OpenAICompatibleProvider(
        profiles,
        transports={role: recorder.transport for role, recorder in recorders.items()},
        capacity=capacity,
        sleep=no_sleep if sleep is None else sleep,
        clock=clock,
    )
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, raise_app_exceptions=raise_app_exceptions
            ),
            base_url="http://testserver",
        ) as client:
            yield Harness(provider=provider, recorders=recorders, app=app, client=client)


def assert_app_error(error: BaseException, *, status: int, code: str) -> None:
    spec = ERRORS[code]
    assert error.code == code
    assert error.status == status
    assert error.message == spec.message
    assert error.retryable == spec.retryable
