"""IT12-01..IT12-28: four-role transport, auth, output modes and status mapping.

Every case resolves a real `Settings` from a complete isolated environment, runs
the real app lifespan and exercises the shipped `OpenAICompatibleProvider`
against one real `httpx.AsyncClient` per role wired to a per-role
`httpx.MockTransport` (IC §13.1, §13.2 IT12; PC §3). Only the external service is
doubled; URL joining, per-role headers, request serialization, strict parsing,
retry/repair budgets and status-derived error mapping are production code.

No credential or developer environment value is read: each case passes an
explicit mapping to `Settings.from_env`.
"""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Callable

import httpx
import pytest

from app.config import Settings
from app.errors import ERRORS
from app.main import create_app
from app.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    KNOWLEDGE_PROMPT,
    serialize_context,
)
from app.providers import OpenAICompatibleProvider, REPAIR_LINE, SCHEMA_INSTRUCTION
from app.schemas import (
    Extraction,
    ExtractionContext,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
    Slots,
)
from tests.integration.test_it02_intake import start_session, turn_body
from tests.integration.test_it08_transcription import (
    RoleTransport,
    assert_error,
    json_wire,
    no_sleep,
    snapshot,
    streamed_wire,
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

ANSWER_PAYLOAD = {
    "answerable": True,
    "answer": "You can request a return within 30 calendar days of delivery.",
    "cited_ids": ["faq-returns"],
}

SOURCE = KnowledgeRecord(
    id="faq-returns",
    title="Returns and refunds",
    keywords=["return", "refund"],
    body="An unused item can be requested for return within 30 calendar days.",
)

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00finite-mp3-fixture-bytes"


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
        frustration_active=False,
    )


def knowledge_context() -> KnowledgeContext:
    return KnowledgeContext(
        question="Can I return an unused item?",
        kb_version="2026-10-22",
        sources=[SOURCE],
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


def status_responder(status: int, body: bytes = b'{"error":"denied"}', headers=None):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_wire([body], status=status, headers=headers)[0]

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


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.index = 0

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


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


def expect_error(error: BaseException, *, status: int, code: str) -> None:
    assert_app_error(error, status=status, code=code)


SENTIMENT_ENUM = ["positive", "neutral", "negative", "uncertain"]
FRUSTRATION_ENUM = ["none", "present", "uncertain"]


def assert_emotion_schema(schema: dict) -> None:
    """The extended extraction schema carries both required emotion labels."""

    assert schema["properties"]["sentiment"]["enum"] == SENTIMENT_ENUM
    assert schema["properties"]["frustration"]["enum"] == FRUSTRATION_ENUM
    assert {"sentiment", "frustration"} <= set(schema["required"])


# IT12-01..IT12-03: output modes ---------------------------------------------


async def case_it12_01(session_dir):
    settings = settings_from(env(session_dir))
    assert settings.llm.output_mode == "json_schema"
    assert settings.rag.output_mode == "json_schema"
    async with adapter_app(
        settings,
        {"llm": chat_valid(), "rag": chat_valid(ANSWER_PAYLOAD)},
    ) as harness:
        extraction = await harness.provider.extract(extraction_context())
        answer = await harness.provider.answer_knowledge(knowledge_context())

        assert extraction == Extraction.model_validate(EXTRACTION_PAYLOAD)
        assert answer == KnowledgeAnswer.model_validate(ANSWER_PAYLOAD)
        assert len(harness.recorders["llm"].requests) == 1
        assert len(harness.recorders["rag"].requests) == 1

        llm_payload = json.loads(harness.recorders["llm"].requests[0].content)
        assert_emotion_schema(llm_payload["response_format"]["json_schema"]["schema"])
        assert llm_payload == {
            "model": "llm-model",
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": serialize_context(extraction_context())},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 1200,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "strict": True,
                    "schema": Extraction.model_json_schema(),
                },
            },
        }
        rag_payload = json.loads(harness.recorders["rag"].requests[0].content)
        assert rag_payload == {
            "model": "rag-model",
            "messages": [
                {"role": "system", "content": KNOWLEDGE_PROMPT},
                {"role": "user", "content": serialize_context(knowledge_context())},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "knowledge_answer",
                    "strict": True,
                    "schema": KnowledgeAnswer.model_json_schema(),
                },
            },
        }


async def case_it12_02(session_dir):
    settings = settings_from(
        env(session_dir, LLM_OUTPUT_MODE="json_object", RAG_OUTPUT_MODE="json_object")
    )
    async with adapter_app(
        settings,
        {"llm": chat_valid(), "rag": chat_valid(ANSWER_PAYLOAD)},
    ) as harness:
        await harness.provider.extract(extraction_context())
        await harness.provider.answer_knowledge(knowledge_context())

        llm_payload = json.loads(harness.recorders["llm"].requests[0].content)
        rag_payload = json.loads(harness.recorders["rag"].requests[0].content)
        assert_emotion_schema(
            json.loads(llm_payload["messages"][0]["content"].split(SCHEMA_INSTRUCTION, 1)[1])
        )
        assert llm_payload["response_format"] == {"type": "json_object"}
        assert rag_payload["response_format"] == {"type": "json_object"}
        assert llm_payload["messages"][0]["content"] == (
            EXTRACTION_SYSTEM_PROMPT
            + "\n\n"
            + SCHEMA_INSTRUCTION
            + json.dumps(Extraction.model_json_schema(), ensure_ascii=False)
        )
        assert rag_payload["messages"][0]["content"] == (
            KNOWLEDGE_PROMPT
            + "\n\n"
            + SCHEMA_INSTRUCTION
            + json.dumps(KnowledgeAnswer.model_json_schema(), ensure_ascii=False)
        )
        assert llm_payload["max_tokens"] == 1200
        assert rag_payload["max_tokens"] == 700
        assert len(harness.recorders["llm"].requests) == 1
        assert len(harness.recorders["rag"].requests) == 1


async def case_it12_03(session_dir):
    settings = settings_from(
        env(session_dir, LLM_OUTPUT_MODE="prompt_json", RAG_OUTPUT_MODE="prompt_json")
    )
    prose = "Here is the result: " + json.dumps(EXTRACTION_PAYLOAD)

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        content = json.dumps(EXTRACTION_PAYLOAD) if attempt == 1 else prose
        return json_wire(chat_envelope(content))

    async with adapter_app(settings, {"llm": responder}) as harness:
        extraction = await harness.provider.extract(extraction_context())
        assert extraction == Extraction.model_validate(EXTRACTION_PAYLOAD)
        payload = json.loads(harness.recorders["llm"].requests[0].content)
        assert "response_format" not in payload
        assert_emotion_schema(
            json.loads(payload["messages"][0]["content"].split(SCHEMA_INSTRUCTION, 1)[1])
        )
        assert payload["messages"][0]["content"] == (
            EXTRACTION_SYSTEM_PROMPT
            + "\n\n"
            + SCHEMA_INSTRUCTION
            + json.dumps(Extraction.model_json_schema(), ensure_ascii=False)
        )
        assert len(harness.recorders["llm"].requests) == 1

        # No substring/regex extraction: prose around a valid JSON object is
        # unusable, so the bounded repair runs and the operation fails.
        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(
            excinfo.value, status=502, code="invalid_provider_response"
        )
        assert len(harness.recorders["llm"].requests) == 3
        second = json.loads(harness.recorders["llm"].requests[1].content)
        third = json.loads(harness.recorders["llm"].requests[2].content)
        assert "response_format" not in second
        assert REPAIR_LINE not in second["messages"][0]["content"]
        assert REPAIR_LINE in third["messages"][0]["content"]


# IT12-04..IT12-09: four independent roles, auth and RAG inheritance ----------


async def case_it12_04(session_dir):
    settings = settings_from(env(session_dir))
    async with adapter_app(
        settings,
        {
            "llm": chat_valid(),
            "rag": chat_valid(ANSWER_PAYLOAD),
            "stt": lambda request, attempt: json_wire({"text": "transcribed"}),
            "tts": lambda request, attempt: httpx.Response(
                200, stream=httpx.ByteStream(MP3), headers={"content-type": "audio/mpeg"}
            ),
        },
    ) as harness:
        await harness.provider.extract(extraction_context())
        await harness.provider.answer_knowledge(knowledge_context())
        await harness.provider.transcribe(b"RIFF-validated-audio")
        await harness.provider.synthesize("Saved reply text.")

        expected = {
            "llm": ("http://llm.test/v1/chat/completions", "llm-model", "Bearer llm-token"),
            "rag": ("http://rag.test/v1/chat/completions", "rag-model", "Bearer rag-token"),
            "stt": ("http://stt.test/v1/audio/transcriptions", "stt-model", "Bearer stt-token"),
            "tts": ("http://tts.test/v1/audio/speech", "tts-model", "Bearer tts-token"),
        }
        tokens = {
            "llm": "llm-token",
            "rag": "rag-token",
            "stt": "stt-token",
            "tts": "tts-token",
        }
        for role, (url, model, authorization) in expected.items():
            requests = harness.recorders[role].requests
            assert len(requests) == 1
            request = requests[0]
            assert str(request.url) == url
            assert request.headers["authorization"] == authorization
            assert request.headers["accept-encoding"] == "identity"
            assert request.method == "POST"
            for other_role, token in tokens.items():
                if other_role == role:
                    continue
                assert all(token not in value for value in request.headers.values())
            if role in ("llm", "rag"):
                assert json.loads(request.content)["model"] == model
            elif role == "stt":
                assert b'name="model"' in request.content
                assert model.encode() in request.content
            else:
                assert json.loads(request.content)["model"] == model


async def case_it12_05(session_dir):
    settings = settings_from(
        env(
            session_dir,
            LLM_BASE_URL="http://shared.test/v1",
            RAG_BASE_URL="http://shared.test/v1",
            STT_BASE_URL="http://shared.test/v1",
            TTS_BASE_URL="http://shared.test/v1",
        )
    )
    async with adapter_app(
        settings,
        {
            "llm": chat_valid(),
            "rag": chat_valid(ANSWER_PAYLOAD),
            "stt": lambda request, attempt: json_wire({"text": "transcribed"}),
            "tts": lambda request, attempt: httpx.Response(
                200, stream=httpx.ByteStream(MP3), headers={"content-type": "audio/mpeg"}
            ),
        },
    ) as harness:
        await harness.provider.extract(extraction_context())
        await harness.provider.answer_knowledge(knowledge_context())
        await harness.provider.transcribe(b"RIFF-audio")
        await harness.provider.synthesize("Saved reply.")

        clients = list(harness.provider._clients.values())
        assert len(clients) == 4
        assert len({id(client) for client in clients}) == 4

        for role, model, token in (
            ("llm", "llm-model", "llm-token"),
            ("rag", "rag-model", "rag-token"),
            ("stt", "stt-model", "stt-token"),
            ("tts", "tts-model", "tts-token"),
        ):
            request = harness.recorders[role].requests[0]
            assert request.url.host == "shared.test"
            assert request.headers["authorization"] == f"Bearer {token}"
            if role in ("llm", "rag", "tts"):
                assert json.loads(request.content)["model"] == model

    # All four clients are closed exactly once by the lifespan, and close() is
    # idempotent: a second close neither re-closes nor fails.
    assert all(client.is_closed for client in clients)
    await harness.provider.close()
    assert all(client.is_closed for client in clients)
    assert harness.provider._closed is True


async def case_it12_06(session_dir):
    settings = settings_from(
        env(
            session_dir,
            RAG_BASE_URL=None,
            RAG_MODEL=None,
            RAG_AUTH_MODE=None,
            RAG_API_KEY=None,
            RAG_OUTPUT_MODE=None,
            OPENAI_API_KEY="sk-developer-sentinel",
            PROVIDER_API_KEY="global-key-sentinel",
        )
    )
    assert settings.rag == settings.llm
    async with adapter_app(settings, {"rag": chat_valid(ANSWER_PAYLOAD)}) as harness:
        answer = await harness.provider.answer_knowledge(knowledge_context())
        assert answer == KnowledgeAnswer.model_validate(ANSWER_PAYLOAD)
        requests = harness.recorders["rag"].requests
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "http://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer llm-token"
        payload = json.loads(request.content)
        assert payload["model"] == "llm-model"
        assert payload["response_format"]["json_schema"]["name"] == "knowledge_answer"
        assert payload["max_tokens"] == 700
        assert all(
            "sk-developer-sentinel" not in value
            and "global-key-sentinel" not in value
            for value in request.headers.values()
        )


async def case_it12_07(session_dir, monkeypatch):
    partial = env(
        session_dir,
        RAG_BASE_URL="http://rag.test/v1",
        RAG_MODEL=None,
        RAG_AUTH_MODE=None,
        RAG_API_KEY=None,
        RAG_OUTPUT_MODE=None,
    )
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(partial)
    assert str(excinfo.value) == (
        "configuration_invalid: Check required provider and model settings."
    )

    apply_env(monkeypatch, partial)
    app = create_app()
    with pytest.raises(RuntimeError) as excinfo:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan reached readiness with a partial RAG profile")
    assert str(excinfo.value) == (
        "configuration_invalid: Check required provider and model settings."
    )
    assert getattr(app.state, "ready", False) is False
    assert not hasattr(app.state, "provider")
    assert not hasattr(app.state, "engine")


async def case_it12_08(session_dir):
    settings = settings_from(
        env(
            session_dir,
            LLM_AUTH_MODE="none",
            LLM_API_KEY=None,
            RAG_AUTH_MODE="none",
            RAG_API_KEY=None,
            STT_AUTH_MODE="none",
            STT_API_KEY=None,
            TTS_AUTH_MODE="none",
            TTS_API_KEY=None,
            OPENAI_API_KEY="sk-developer-sentinel",
            PROVIDER_API_KEY="global-key-sentinel",
        )
    )
    async with adapter_app(
        settings,
        {
            "llm": chat_valid(),
            "rag": chat_valid(ANSWER_PAYLOAD),
            "stt": lambda request, attempt: json_wire({"text": "transcribed"}),
            "tts": lambda request, attempt: httpx.Response(
                200, stream=httpx.ByteStream(MP3), headers={"content-type": "audio/mpeg"}
            ),
        },
    ) as harness:
        await harness.provider.extract(extraction_context())
        await harness.provider.answer_knowledge(knowledge_context())
        await harness.provider.transcribe(b"RIFF-audio")
        await harness.provider.synthesize("Saved reply.")
        for role in ROLES:
            request = harness.recorders[role].requests[0]
            assert "authorization" not in request.headers
            assert all(
                "sk-developer-sentinel" not in value
                and "global-key-sentinel" not in value
                for value in request.headers.values()
            )


async def case_it12_09(session_dir):
    settings = settings_from(
        env(
            session_dir,
            LLM_API_KEY="token-llm",
            RAG_API_KEY="token-rag",
            STT_API_KEY="token-stt",
            TTS_API_KEY="token-tts",
        )
    )
    async with adapter_app(
        settings,
        {
            "llm": chat_valid(),
            "rag": chat_valid(ANSWER_PAYLOAD),
            "stt": lambda request, attempt: json_wire({"text": "transcribed"}),
            "tts": lambda request, attempt: httpx.Response(
                200, stream=httpx.ByteStream(MP3), headers={"content-type": "audio/mpeg"}
            ),
        },
    ) as harness:
        await harness.provider.extract(extraction_context())
        await harness.provider.answer_knowledge(knowledge_context())
        await harness.provider.transcribe(b"RIFF-audio")
        await harness.provider.synthesize("Saved reply.")
        tokens = {
            "llm": "token-llm",
            "rag": "token-rag",
            "stt": "token-stt",
            "tts": "token-tts",
        }
        for role, token in tokens.items():
            request = harness.recorders[role].requests[0]
            assert request.headers["authorization"] == f"Bearer {token}"
            for other_role, other_token in tokens.items():
                if other_role != role:
                    assert all(
                        other_token not in value for value in request.headers.values()
                    )


# IT12-10..IT12-19: redirect and HTTP status mapping -------------------------


async def case_it12_10(session_dir):
    settings = settings_from(env(session_dir))
    redirect = lambda request, attempt: httpx.Response(  # noqa: E731
        302,
        headers={"location": "http://elsewhere.test/v1/chat/completions"},
        stream=httpx.ByteStream(b"moved"),
    )
    async with adapter_app(settings, {"llm": redirect}) as harness:
        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_configuration")
        requests = harness.recorders["llm"].requests
        assert len(requests) == 1
        assert str(requests[0].url) == "http://llm.test/v1/chat/completions"
        assert harness.recorders["rag"].requests == []


async def case_it12_configuration_status(status, session_dir, caplog):
    settings = settings_from(env(session_dir))
    secret = "sk-leaked-credential-sentinel"
    body = json.dumps({"error": secret}).encode("utf-8")
    async with adapter_app(settings, {"llm": status_responder(status, body)}) as harness:
        with caplog.at_level("DEBUG"):
            with pytest.raises(Exception) as excinfo:
                await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_configuration")
        assert len(harness.recorders["llm"].requests) == 1
        assert secret not in str(excinfo.value)
        assert secret not in caplog.text
        payload = json.loads(harness.recorders["llm"].requests[0].content)
        if status == 422:
            # No downgrade: the structured mode and schema are unchanged.
            assert payload["response_format"]["type"] == "json_schema"
            assert payload["response_format"]["json_schema"]["name"] == "extraction"
            assert len(harness.recorders["llm"].requests) == 1


async def case_it12_18(session_dir):
    settings = settings_from(env(session_dir))
    unavailable = status_responder(429, b'{"error":"rate limited"}')
    async with adapter_app(
        settings, {"llm": unavailable, "stt": unavailable, "tts": unavailable}
    ) as harness:
        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(harness.recorders["llm"].requests) == 2

        with pytest.raises(Exception) as excinfo:
            await harness.provider.transcribe(b"RIFF-audio")
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(harness.recorders["stt"].requests) == 2

        with pytest.raises(Exception) as excinfo:
            await harness.provider.synthesize("Saved reply.")
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(harness.recorders["tts"].requests) == 1


async def case_it12_19(session_dir):
    settings = settings_from(env(session_dir))
    unavailable = status_responder(500, b'{"error":"server error"}')
    async with adapter_app(
        settings, {"llm": unavailable, "stt": unavailable}
    ) as harness:
        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(harness.recorders["llm"].requests) == 2

        with pytest.raises(Exception) as excinfo:
            await harness.provider.transcribe(b"RIFF-audio")
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(harness.recorders["stt"].requests) == 2

    # The retry pause is bounded by the shared outer deadline: with 0.1 s left
    # after the first 500, jitter sleeps at most 0.1 s and the second attempt
    # still runs inside the budget.
    clock = SequenceClock([1000.0, 1000.0, 1024.9, 1024.9])
    sleep = SleepRecorder()
    async with adapter_app(
        settings, {"llm": unavailable}, clock=clock, sleep=sleep
    ) as harness:
        with pytest.raises(Exception) as excinfo:
            await harness.provider.extract(extraction_context())
        assert_app_error(excinfo.value, status=503, code="provider_unavailable")
        assert len(sleep.calls) == 1
        assert 0 < sleep.calls[0] <= 0.1
        assert len(harness.recorders["llm"].requests) == 2


# IT12-20..IT12-28: unusable 2xx payloads, bounded attempts, no commit --------


INVALID_ENVELOPES = {
    "malformed": b"{not-json",
    "empty_choices": {"choices": []},
    "tool_calls": {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(EXTRACTION_PAYLOAD),
                    "tool_calls": [{"id": "call-1", "type": "function"}],
                },
            }
        ]
    },
    "refusal": {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(EXTRACTION_PAYLOAD),
                    "refusal": "I cannot help with that.",
                },
            }
        ]
    },
    "length": chat_envelope(json.dumps(EXTRACTION_PAYLOAD), finish_reason="length"),
    "null_content": chat_envelope(None),
    "missing_choices": {"model": "llm-model"},
    "null_finish_reason": chat_envelope(
        json.dumps(EXTRACTION_PAYLOAD), finish_reason=None
    ),
    "multipart_content": chat_envelope(
        [{"type": "text", "text": json.dumps(EXTRACTION_PAYLOAD)}]
    ),
}


def invalid_responder(kind: str):
    payload = INVALID_ENVELOPES[kind]

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(
                200,
                stream=httpx.ByteStream(payload),
                headers={"content-type": "application/json"},
            )
        return json_wire(payload)

    return responder


async def assert_invalid_success(settings, session_dir, kind: str):
    async with adapter_app(
        settings, {"llm": invalid_responder(kind)}
    ) as harness:
        session_id = await start_session(harness.client)
        before = snapshot(session_dir, session_id)
        response = await harness.client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body("My order NS-000123 arrived broken.", 0),
        )
        assert_error(response, status=502, code="invalid_provider_response")
        assert len(harness.recorders["llm"].requests) == 2
        first, second = harness.recorders["llm"].requests
        first_system = json.loads(first.content)["messages"][0]["content"]
        second_system = json.loads(second.content)["messages"][0]["content"]
        assert REPAIR_LINE not in first_system
        assert REPAIR_LINE in second_system
        assert snapshot(session_dir, session_id) == before


async def case_it12_20(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "malformed")


async def case_it12_21(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "empty_choices"
    )


async def case_it12_22(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "tool_calls")


async def case_it12_23(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "refusal")


async def case_it12_24(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "length")


async def case_it12_25(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "null_content"
    )


async def case_it12_26(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "missing_choices"
    )


async def case_it12_27(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "null_finish_reason"
    )


async def case_it12_28(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "multipart_content"
    )


CASES = {
    "IT12-01": case_it12_01,
    "IT12-02": case_it12_02,
    "IT12-03": case_it12_03,
    "IT12-04": case_it12_04,
    "IT12-05": case_it12_05,
    "IT12-06": case_it12_06,
    "IT12-07": case_it12_07,
    "IT12-08": case_it12_08,
    "IT12-09": case_it12_09,
    "IT12-10": case_it12_10,
    "IT12-11": partial(case_it12_configuration_status, 400),
    "IT12-12": partial(case_it12_configuration_status, 401),
    "IT12-13": partial(case_it12_configuration_status, 403),
    "IT12-14": partial(case_it12_configuration_status, 404),
    "IT12-15": partial(case_it12_configuration_status, 405),
    "IT12-16": partial(case_it12_configuration_status, 415),
    "IT12-17": partial(case_it12_configuration_status, 422),
    "IT12-18": case_it12_18,
    "IT12-19": case_it12_19,
    "IT12-20": case_it12_20,
    "IT12-21": case_it12_21,
    "IT12-22": case_it12_22,
    "IT12-23": case_it12_23,
    "IT12-24": case_it12_24,
    "IT12-25": case_it12_25,
    "IT12-26": case_it12_26,
    "IT12-27": case_it12_27,
    "IT12-28": case_it12_28,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_it12_transport(case_id, session_dir, monkeypatch, caplog):
    case = CASES[case_id]
    kwargs = {"session_dir": session_dir}
    parameters = inspect.signature(case).parameters
    if "monkeypatch" in parameters:
        kwargs["monkeypatch"] = monkeypatch
    if "caplog" in parameters:
        kwargs["caplog"] = caplog
    await case(**kwargs)
