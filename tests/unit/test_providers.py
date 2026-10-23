"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Callable

import httpx
import pytest

from app.config import ServiceProfile, SpeechProfile, TextProfile
from app.errors import AppError
from app.providers import OpenAICompatibleProvider
from app.schemas import (
    Extraction,
    ExtractionContext,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
    Slots,
)

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

SOURCE = KnowledgeRecord(
    id="faq-returns",
    title="Returns and refunds",
    keywords=["return", "refund"],
    body="An unused item can be requested for return within 30 calendar days.",
)


def extraction_context(text: str = "My order NS-000123 arrived broken.") -> ExtractionContext:
    return ExtractionContext(
        slots=Slots(
            order_number=None, category=None, description=None, urgency=None
        ),
        pending_clarifications={},
        recent_messages=[],
        current_message=text,
        status="collecting",
        frustration_active=False,
    )


def knowledge_context() -> KnowledgeContext:
    return KnowledgeContext(
        question="Can I return an unused item?",
        kb_version="2026-10-22",
        sources=[SOURCE],
    )


def chat_envelope(content: object, finish_reason: object = "stop", **message_extra: object):
    message = {"content": content, **message_extra}
    return {"choices": [{"finish_reason": finish_reason, "message": message}]}


def streamed_response(
    body: bytes, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    """A real streaming response: not pre-consumed, so aiter_raw works."""

    return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers or {})


def json_response(payload: object, status: int = 200, headers: dict[str, str] | None = None):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return streamed_response(body.encode("utf-8"), status, headers)


class RoleTransport:
    """MockTransport that records each request and delegates to a responder."""

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


# --------------------------------------------------------------------------
# Slice A: role selection, auth, URL joining, redirects, status mapping, retry
# --------------------------------------------------------------------------


async def test_extract_uses_only_the_llm_role_and_its_model():
    provider, recorders = build_provider({"llm": extraction_responder()})

    result = await provider.extract(extraction_context())

    assert isinstance(result, Extraction)
    assert len(recorders["llm"].requests) == 1
    request = recorders["llm"].requests[0]
    assert str(request.url) == "http://llm.test/v1/chat/completions"
    assert request.method == "POST"
    assert json.loads(request.content)["model"] == "llm-model"
    for role in ("rag", "stt", "tts"):
        assert recorders[role].requests == []


async def test_answer_knowledge_uses_only_the_rag_role_and_its_model():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response(chat_envelope(json.dumps(ANSWER_PAYLOAD)))

    provider, recorders = build_provider({"rag": responder})

    result = await provider.answer_knowledge(knowledge_context())

    assert result.answerable is True
    assert str(recorders["rag"].requests[0].url) == "http://rag.test/v1/chat/completions"
    assert json.loads(recorders["rag"].requests[0].content)["model"] == "rag-model"
    assert recorders["llm"].requests == []


async def test_transcribe_uses_only_the_stt_role_and_its_model():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"text": "My order arrived broken."})

    provider, recorders = build_provider({"stt": responder})

    text = await provider.transcribe(b"RIFF-audio-bytes")

    assert text == "My order arrived broken."
    request = recorders["stt"].requests[0]
    assert str(request.url) == "http://stt.test/v1/audio/transcriptions"
    assert b'name="model"' in request.content
    assert b"stt-model" in request.content
    assert recorders["llm"].requests == []


async def test_synthesize_uses_only_the_tts_role_and_its_model():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(b"ID3-mp3-bytes", 200, {"content-type": "audio/mpeg"})

    provider, recorders = build_provider({"tts": responder})

    audio = await provider.synthesize("Saved locally.")

    assert audio.data == b"ID3-mp3-bytes"
    assert audio.media_type == "audio/mpeg"
    request = recorders["tts"].requests[0]
    assert str(request.url) == "http://tts.test/v1/audio/speech"
    assert json.loads(request.content)["model"] == "tts-model"
    assert recorders["rag"].requests == []


async def test_none_auth_sends_no_authorization_header_on_any_role():
    def stt_responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"text": "hello"})

    def tts_responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(b"mp3", 200, {"content-type": "audio/mpeg"})

    provider, recorders = build_provider(
        {
            "llm": extraction_responder(),
            "rag": lambda request, attempt: json_response(
                chat_envelope(json.dumps(ANSWER_PAYLOAD))
            ),
            "stt": stt_responder,
            "tts": tts_responder,
        }
    )

    await provider.extract(extraction_context())
    await provider.answer_knowledge(knowledge_context())
    await provider.transcribe(b"wav")
    await provider.synthesize("hello")

    for role in ROLES:
        for request in recorders[role].requests:
            assert "authorization" not in request.headers


async def test_bearer_auth_sends_only_that_roles_own_key():
    profiles = {
        "llm": replace(LLM_PROFILE, auth_mode="bearer", api_key="llm-key"),
        "rag": replace(RAG_PROFILE, auth_mode="bearer", api_key="rag-key"),
        "stt": replace(STT_PROFILE, auth_mode="bearer", api_key="stt-key"),
        "tts": replace(TTS_PROFILE, auth_mode="bearer", api_key="tts-key"),
    }

    def stt_responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"text": "hello"})

    def tts_responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(b"mp3", 200, {"content-type": "audio/mpeg"})

    provider, recorders = build_provider(
        {
            "llm": extraction_responder(),
            "rag": lambda request, attempt: json_response(
                chat_envelope(json.dumps(ANSWER_PAYLOAD))
            ),
            "stt": stt_responder,
            "tts": tts_responder,
        },
        profiles=profiles,
    )

    await provider.extract(extraction_context())
    await provider.answer_knowledge(knowledge_context())
    await provider.transcribe(b"wav")
    await provider.synthesize("hello")

    expected = {"llm": "llm-key", "rag": "rag-key", "stt": "stt-key", "tts": "tts-key"}
    for role in ROLES:
        request = recorders[role].requests[0]
        assert request.headers["authorization"] == f"Bearer {expected[role]}"
        for other_role, other_key in expected.items():
            if other_role != role:
                assert other_key.encode() not in request.content


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://llm.test/v1", "http://llm.test/v1/chat/completions"),
        ("http://llm.test/v1/", "http://llm.test/v1/chat/completions"),
        ("http://llm.test/v1///", "http://llm.test/v1/chat/completions"),
        ("http://llm.test", "http://llm.test/chat/completions"),
    ],
)
async def test_base_url_trailing_slashes_are_stripped_without_double_v1(
    base_url: str, expected: str
):
    profile = replace(LLM_PROFILE, base_url=base_url)
    provider, recorders = build_provider(
        {"llm": extraction_responder()}, profiles={"llm": profile, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": TTS_PROFILE}
    )

    await provider.extract(extraction_context())

    assert str(recorders["llm"].requests[0].url) == expected


async def test_clients_are_constructed_without_redirects_env_proxy_or_compression():
    provider, _ = build_provider({"llm": extraction_responder()})

    for role in ROLES:
        client = provider._clients[role]
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.headers["accept-encoding"] == "identity"
        assert client.timeout.connect is not None
        assert client.timeout.read is not None

    await provider.close()


async def test_redirect_is_nonretryable_provider_configuration_with_one_attempt():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(b"", 302, {"location": "http://elsewhere.test/v1"})

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_configuration"
    assert caught.value.status == 503
    assert caught.value.retryable is False
    assert len(recorders["llm"].requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 415, 422])
async def test_configuration_statuses_are_nonretryable_with_one_attempt(status: int):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"error": "denied"}, status=status)

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert len(recorders["llm"].requests) == 1


async def test_429_twice_retries_once_with_jitter_then_provider_unavailable():
    sleeps = SleepRecorder()

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"error": "busy"}, status=429)

    provider, recorders = build_provider({"llm": responder}, sleep=sleeps)

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_unavailable"
    assert caught.value.status == 503
    assert caught.value.retryable is True
    assert len(recorders["llm"].requests) == 2
    assert len(sleeps.calls) == 1
    assert 0.25 <= sleeps.calls[0] <= 0.5


async def test_5xx_twice_retries_once_then_provider_unavailable():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"error": "boom"}, status=500)

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["llm"].requests) == 2


async def test_connection_failure_twice_retries_once_then_provider_unavailable():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["llm"].requests) == 2


async def test_transient_failure_then_valid_response_succeeds_on_second_attempt():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return json_response({"error": "busy"}, status=429)
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    provider, recorders = build_provider({"llm": responder})

    result = await provider.extract(extraction_context())

    assert result.candidates.order_number == "NS-000123"
    assert len(recorders["llm"].requests) == 2


async def test_exhausted_deadline_maps_provider_timeout_without_a_second_wire_call():
    clock = Clock()
    sleeps = SleepRecorder()

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        clock.now += 30.0
        return json_response({"error": "busy"}, status=429)

    provider, recorders = build_provider({"llm": responder}, clock=clock, sleep=sleeps)

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_timeout"
    assert caught.value.status == 504
    assert len(recorders["llm"].requests) == 1
    assert sleeps.calls == []


# --------------------------------------------------------------------------
# Slice B: output modes, exact payloads, bounded bodies, tolerance, repair
# --------------------------------------------------------------------------

from app.prompts import EXTRACTION_SYSTEM_PROMPT, KNOWLEDGE_PROMPT, serialize_context  # noqa: E402
from app.providers import REPAIR_LINE, SCHEMA_INSTRUCTION  # noqa: E402


def padded_envelope(target_bytes: int) -> str:
    base = chat_envelope(json.dumps(EXTRACTION_PAYLOAD))
    base["pad"] = ""
    pad = target_bytes - len(json.dumps(base).encode("utf-8"))
    assert pad >= 0
    base["pad"] = "x" * pad
    raw = json.dumps(base)
    assert len(raw.encode("utf-8")) == target_bytes
    return raw


def single_response(body: str | bytes, status: int = 200, headers: dict[str, str] | None = None):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        content = body.encode("utf-8") if isinstance(body, str) else body
        return streamed_response(content, status, headers)

    return responder


async def test_json_schema_mode_sends_the_exact_extraction_request():
    provider, recorders = build_provider({"llm": extraction_responder()})

    await provider.extract(extraction_context())

    payload = json.loads(recorders["llm"].requests[0].content)
    assert payload == {
        "model": "llm-model",
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": serialize_context(extraction_context()),
            },
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


async def test_json_schema_mode_sends_the_exact_knowledge_request():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response(chat_envelope(json.dumps(ANSWER_PAYLOAD)))

    provider, recorders = build_provider({"rag": responder})

    await provider.answer_knowledge(knowledge_context())

    payload = json.loads(recorders["rag"].requests[0].content)
    assert payload == {
        "model": "rag-model",
        "messages": [
            {"role": "system", "content": KNOWLEDGE_PROMPT},
            {
                "role": "user",
                "content": serialize_context(knowledge_context()),
            },
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


@pytest.mark.parametrize("mode", ["json_object", "prompt_json"])
async def test_schema_instruction_modes_append_the_exact_instruction(mode: str):
    profile = replace(LLM_PROFILE, output_mode=mode)
    provider, recorders = build_provider(
        {"llm": extraction_responder()},
        profiles={"llm": profile, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": TTS_PROFILE},
    )

    await provider.extract(extraction_context())

    payload = json.loads(recorders["llm"].requests[0].content)
    expected_system = (
        EXTRACTION_SYSTEM_PROMPT
        + "\n\n"
        + SCHEMA_INSTRUCTION
        + json.dumps(Extraction.model_json_schema(), ensure_ascii=False)
    )
    assert payload["messages"][0]["content"] == expected_system
    assert payload["max_tokens"] == 1200
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    if mode == "json_object":
        assert payload["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in payload


async def test_success_json_exactly_one_mib_is_accepted():
    body = padded_envelope(1024 * 1024)
    provider, recorders = build_provider({"llm": single_response(body)})

    result = await provider.extract(extraction_context())

    assert result.candidates.order_number == "NS-000123"
    assert len(recorders["llm"].requests) == 1


async def test_success_json_one_byte_over_the_cap_fails_after_bounded_attempts():
    body = padded_envelope(1024 * 1024 + 1)
    provider, recorders = build_provider({"llm": single_response(body)})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert caught.value.status == 502
    assert caught.value.retryable is True
    assert len(recorders["llm"].requests) == 2


@pytest.mark.parametrize("size", [64 * 1024, 64 * 1024 + 1])
async def test_error_body_bound_keeps_status_derived_configuration_mapping(size: int):
    provider, recorders = build_provider(
        {"llm": single_response(b"e" * size, status=401)}
    )

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert "e" * 100 not in caught.value.message
    assert len(recorders["llm"].requests) == 1


async def test_oversized_retryable_error_body_still_retries_then_unavailable():
    provider, recorders = build_provider(
        {"llm": single_response(b"e" * (64 * 1024 + 1), status=429)}
    )

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["llm"].requests) == 2


async def test_gzip_success_despite_identity_is_invalid_without_decompression():
    import gzip

    compressed = gzip.compress(
        json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))).encode("utf-8")
    )
    provider, recorders = build_provider(
        {"llm": single_response(compressed, headers={"content-encoding": "gzip"})}
    )

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2
    assert recorders["llm"].requests[0].headers["accept-encoding"] == "identity"


TOLERANCE_ENVELOPES = {
    "finish_reason_length": lambda: chat_envelope(
        json.dumps(EXTRACTION_PAYLOAD), finish_reason="length"
    ),
    "finish_reason_null": lambda: chat_envelope(
        json.dumps(EXTRACTION_PAYLOAD), finish_reason=None
    ),
    "refusal": lambda: chat_envelope(
        json.dumps(EXTRACTION_PAYLOAD), refusal="I cannot help with that."
    ),
    "tool_calls": lambda: chat_envelope(
        json.dumps(EXTRACTION_PAYLOAD), tool_calls=[{"id": "call-1"}]
    ),
    "empty_choices": lambda: {"choices": []},
    "content_null": lambda: chat_envelope(None),
    "multipart_content": lambda: chat_envelope(
        [{"type": "text", "text": json.dumps(EXTRACTION_PAYLOAD)}]
    ),
    "missing_choices": lambda: {"object": "chat.completion"},
}


@pytest.mark.parametrize("case", sorted(TOLERANCE_ENVELOPES))
async def test_unusable_success_payloads_exhaust_attempts_then_502(case: str):
    envelope = TOLERANCE_ENVELOPES[case]()
    provider, recorders = build_provider({"llm": single_response(json.dumps(envelope))})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert caught.value.status == 502
    assert len(recorders["llm"].requests) == 2


async def test_non_json_success_body_is_invalid_provider_response_after_attempts():
    provider, recorders = build_provider({"llm": single_response("not json at all")})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2


async def test_repair_after_schema_failure_sends_fixed_extra_system_line():
    incomplete = {"intent": "provide_details"}

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        content = json.dumps(incomplete if attempt == 1 else EXTRACTION_PAYLOAD)
        return json_response(chat_envelope(content))

    provider, recorders = build_provider({"llm": responder})

    result = await provider.extract(extraction_context())

    assert result.candidates.order_number == "NS-000123"
    assert len(recorders["llm"].requests) == 2
    first = json.loads(recorders["llm"].requests[0].content)
    second = json.loads(recorders["llm"].requests[1].content)
    assert first["messages"][0]["content"] == EXTRACTION_SYSTEM_PROMPT
    assert second["messages"][0]["content"] == EXTRACTION_SYSTEM_PROMPT + "\n" + REPAIR_LINE
    assert second["messages"][1] == first["messages"][1]
    assert second["response_format"] == first["response_format"]
    assert second["max_tokens"] == first["max_tokens"] == 1200


async def test_repair_never_runs_a_third_attempt():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response(chat_envelope(json.dumps({"intent": "provide_details"})))

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2
    assert REPAIR_LINE in json.loads(recorders["llm"].requests[1].content)["messages"][0]["content"]


async def test_knowledge_citation_outside_retrieved_sources_repairs_then_succeeds():
    bad = {"answerable": True, "answer": "Refunds are instant.", "cited_ids": ["faq-unknown"]}

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        content = json.dumps(bad if attempt == 1 else ANSWER_PAYLOAD)
        return json_response(chat_envelope(content))

    provider, recorders = build_provider({"rag": responder})

    result = await provider.answer_knowledge(knowledge_context())

    assert result.cited_ids == ["faq-returns"]
    assert len(recorders["rag"].requests) == 2
    assert REPAIR_LINE in json.loads(recorders["rag"].requests[1].content)["messages"][0]["content"]


async def test_answerable_false_with_text_is_invalid_provider_response():
    bad = {"answerable": False, "answer": "Here is an answer.", "cited_ids": []}
    provider, recorders = build_provider(
        {"rag": single_response(json.dumps(chat_envelope(json.dumps(bad))))}
    )

    with pytest.raises(AppError) as caught:
        await provider.answer_knowledge(knowledge_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["rag"].requests) == 2


# --------------------------------------------------------------------------
# Slice C: STT multipart request, transcript validation and bounded bodies
# --------------------------------------------------------------------------


def transcript_envelope(target_bytes: int | None = None) -> str:
    payload: dict[str, object] = {"text": "My order arrived broken."}
    raw = json.dumps(payload)
    if target_bytes is None:
        return raw
    payload["pad"] = ""
    pad = target_bytes - len(json.dumps(payload).encode("utf-8"))
    assert pad >= 0
    payload["pad"] = "x" * pad
    raw = json.dumps(payload)
    assert len(raw.encode("utf-8")) == target_bytes
    return raw


async def test_transcribe_sends_the_exact_multipart_request():
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "hello"})}
    )

    await provider.transcribe(b"RIFF-validated-wav-bytes")

    request = recorders["stt"].requests[0]
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="file"; filename="upload.wav"' in body
    assert b"Content-Type: audio/wav" in body
    assert b"RIFF-validated-wav-bytes" in body
    assert b'name="model"' in body and b"stt-model" in body
    assert b'name="response_format"' in body and b"json" in body


async def test_transcribe_trims_text_and_ignores_extra_response_fields():
    provider, _ = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "  hello there  ", "language": "en"})}
    )

    assert await provider.transcribe(b"wav") == "hello there"


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}])
async def test_empty_or_nonstring_transcript_is_not_retried(payload: dict):
    provider, recorders = build_provider({"stt": lambda request, attempt: json_response(payload)})

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "empty_transcription"
    assert caught.value.status == 422
    assert caught.value.retryable is False
    assert len(recorders["stt"].requests) == 1


@pytest.mark.parametrize("length,code", [(4000, None), (4001, "transcript_too_long")])
async def test_transcript_length_boundary(length: int, code: str | None):
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "a" * length})}
    )

    if code is None:
        assert await provider.transcribe(b"wav") == "a" * length
        assert len(recorders["stt"].requests) == 1
    else:
        with pytest.raises(AppError) as caught:
            await provider.transcribe(b"wav")
        assert caught.value.code == code
        assert caught.value.status == 422
        assert len(recorders["stt"].requests) == 1


@pytest.mark.parametrize("size", [1024 * 1024, 1024 * 1024 + 1])
async def test_transcript_success_json_one_mib_boundary(size: int):
    provider, recorders = build_provider({"stt": single_response(transcript_envelope(size))})

    if size == 1024 * 1024:
        assert (await provider.transcribe(b"wav")).startswith("My order")
        assert len(recorders["stt"].requests) == 1
    else:
        with pytest.raises(AppError) as caught:
            await provider.transcribe(b"wav")
        assert caught.value.code == "invalid_provider_response"
        assert len(recorders["stt"].requests) == 2


async def test_malformed_transcript_json_retries_once_then_502():
    provider, recorders = build_provider({"stt": single_response("not json")})

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["stt"].requests) == 2


async def test_gzip_transcript_success_is_rejected_without_decompression():
    import gzip

    compressed = gzip.compress(json.dumps({"text": "hello"}).encode("utf-8"))
    provider, recorders = build_provider(
        {"stt": single_response(compressed, headers={"content-encoding": "gzip"})}
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["stt"].requests) == 2


async def test_stt_429_retries_once_then_provider_unavailable():
    sleeps = SleepRecorder()
    provider, recorders = build_provider(
        {"stt": single_response("{}", status=429)}, sleep=sleeps
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["stt"].requests) == 2
    assert len(sleeps.calls) == 1
    assert 0.25 <= sleeps.calls[0] <= 0.5


async def test_stt_401_is_configuration_error_with_one_attempt_and_bounded_body():
    provider, recorders = build_provider(
        {"stt": single_response(b"e" * (64 * 1024 + 1), status=401)}
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert len(recorders["stt"].requests) == 1


# --------------------------------------------------------------------------
# Slice D: TTS single attempt, media type, encoding, 5 MiB streamed cap
# --------------------------------------------------------------------------


def audio_responder(body: bytes, headers: dict[str, str] | None = None, status: int = 200):
    resolved = {"content-type": "audio/mpeg"} if headers is None else headers

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(body, status, resolved)

    return responder


async def test_synthesize_sends_the_exact_speech_request():
    provider, recorders = build_provider({"tts": audio_responder(b"ID3-mp3")})

    result = await provider.synthesize("Saved locally as x.")

    assert result.data == b"ID3-mp3"
    assert result.media_type == "audio/mpeg"

    request = recorders["tts"].requests[0]
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "model": "tts-model",
        "voice": "alloy",
        "input": "Saved locally as x.",
        "response_format": "mp3",
    }


@pytest.mark.parametrize(
    "content_type", ["audio/mpeg", "audio/mp3", "Audio/MPEG", "audio/mpeg; charset=binary"]
)
async def test_accepted_mp3_content_types(content_type: str):
    provider, _ = build_provider({"tts": audio_responder(b"mp3", {"content-type": content_type})})

    assert (await provider.synthesize("hello")).data == b"mp3"


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/json", "audio/wav", "", None]
)
async def test_non_mp3_success_content_is_one_attempt_invalid_response(content_type):
    headers = {} if content_type is None else {"content-type": content_type}
    provider, recorders = build_provider({"tts": audio_responder(b"wav-bytes", headers)})

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert caught.value.status == 502
    assert len(recorders["tts"].requests) == 1


async def test_empty_mp3_body_is_invalid_response_with_one_attempt():
    provider, recorders = build_provider({"tts": audio_responder(b"")})

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


@pytest.mark.parametrize("size", [5 * 1024 * 1024, 5 * 1024 * 1024 + 1])
async def test_mp3_five_mib_boundary(size: int):
    provider, recorders = build_provider({"tts": audio_responder(b"m" * size)})

    if size == 5 * 1024 * 1024:
        assert len((await provider.synthesize("hello")).data) == size
        assert len(recorders["tts"].requests) == 1
    else:
        with pytest.raises(AppError) as caught:
            await provider.synthesize("hello")
        assert caught.value.code == "invalid_provider_response"
        assert len(recorders["tts"].requests) == 1


async def test_gzip_mp3_success_is_rejected_without_decompression():
    import gzip

    provider, recorders = build_provider(
        {"tts": audio_responder(gzip.compress(b"mp3-bytes"), {"content-type": "audio/mpeg", "content-encoding": "gzip"})}
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_mp3_streamed_chunks_are_joined_after_a_bounded_read():
    stream = ChunkedStream([b"ID3", b"-part2", b"-part3"])

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200, stream=stream, headers={"content-type": "audio/mpeg"}
        )

    provider, _ = build_provider({"tts": responder})

    assert (await provider.synthesize("hello")).data == b"ID3-part2-part3"
    assert stream.closed is True


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_tts_transient_failure_is_single_attempt_provider_unavailable(status: int):
    sleeps = SleepRecorder()
    provider, recorders = build_provider(
        {"tts": audio_responder(b"error", {"content-type": "text/plain"}, status=status)},
        sleep=sleeps,
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_unavailable"
    assert caught.value.status == 503
    assert len(recorders["tts"].requests) == 1
    assert sleeps.calls == []


@pytest.mark.parametrize("status", [302, 401, 404, 415])
async def test_tts_configuration_status_is_single_attempt_nonretryable(status: int):
    provider, recorders = build_provider(
        {"tts": audio_responder(b"", {"content-type": "text/plain"}, status=status)}
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert len(recorders["tts"].requests) == 1


async def test_tts_connection_failure_is_single_attempt_provider_unavailable():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, recorders = build_provider({"tts": responder})

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["tts"].requests) == 1


# --------------------------------------------------------------------------
# Slice E: capacity gate, cancellation cleanup, client lifecycle
# --------------------------------------------------------------------------


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

    tasks = [asyncio.create_task(provider.extract(extraction_context())) for _ in range(4)]
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


# --------------------------------------------------------------------------
# Slice F: unexercised failure branches, deadline edges, capacity boundaries
# --------------------------------------------------------------------------


class SequenceClock:
    """Clock returning a fixed script of values; repeats the last one."""

    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.index = 0

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


async def test_text_invalid_utf8_body_is_invalid_provider_response_after_attempts():
    provider, recorders = build_provider({"llm": single_response(b"\xff\xfe\x00\x01")})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2


async def test_text_read_timeout_twice_is_provider_timeout():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    sleeps = SleepRecorder()
    provider, recorders = build_provider({"llm": responder}, sleep=sleeps)

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_timeout"
    assert caught.value.status == 504
    assert len(recorders["llm"].requests) == 2
    assert len(sleeps.calls) == 1


async def test_text_read_timeout_then_success_pauses_once():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    sleeps = SleepRecorder()
    provider, recorders = build_provider({"llm": responder}, sleep=sleeps)

    result = await provider.extract(extraction_context())

    assert isinstance(result, Extraction)
    assert len(recorders["llm"].requests) == 2
    assert len(sleeps.calls) == 1
    assert 0.25 <= sleeps.calls[0] <= 0.5


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, delay: float, body: bytes) -> None:
        self.delay = delay
        self.body = body

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield self.body


async def test_text_asyncio_timeout_then_success_pauses_and_retries():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(
                200,
                stream=SlowStream(0.3, json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))).encode()),
                headers={"content-type": "application/json"},
            )
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    sleeps = SleepRecorder()
    clock = SequenceClock([1000.0, 1024.9, 1024.9, 1024.9])
    provider, recorders = build_provider({"llm": responder}, sleep=sleeps, clock=clock)

    result = await provider.extract(extraction_context())

    assert isinstance(result, Extraction)
    assert len(recorders["llm"].requests) == 2
    assert len(sleeps.calls) == 1


@pytest.mark.parametrize("role", ["llm", "stt", "tts"])
async def test_status_300_is_configuration_error_not_success(role: str):
    if role == "llm":
        responder = single_response(json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))), status=300)
        provider, recorders = build_provider({"llm": responder})
        call = provider.extract(extraction_context())
    elif role == "stt":
        responder = single_response(json.dumps({"text": "hello"}), status=300)
        provider, recorders = build_provider({"stt": responder})
        call = provider.transcribe(b"wav")
    else:
        responder = audio_responder(b"mp3", status=300)
        provider, recorders = build_provider({"tts": responder})
        call = provider.synthesize("hello")

    with pytest.raises(AppError) as caught:
        await call

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert len(recorders[role].requests) == 1


async def test_identity_content_encoding_is_accepted():
    body = json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))
    provider, recorders = build_provider(
        {"llm": single_response(body, headers={"content-encoding": "IDENTITY"})}
    )

    result = await provider.extract(extraction_context())

    assert isinstance(result, Extraction)
    assert len(recorders["llm"].requests) == 1


async def test_text_zero_remaining_deadline_raises_timeout_without_a_wire_call():
    provider, recorders = build_provider(
        {"llm": extraction_responder()}, clock=SequenceClock([1000.0, 1025.0])
    )

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_timeout"
    assert recorders["llm"].requests == []


async def test_text_one_second_remaining_still_runs_the_attempt():
    provider, recorders = build_provider(
        {"llm": extraction_responder()}, clock=SequenceClock([1000.0, 1024.0])
    )

    assert isinstance(await provider.extract(extraction_context()), Extraction)
    assert len(recorders["llm"].requests) == 1


async def test_text_pause_at_zero_remaining_is_timeout_without_sleeping():
    sleeps = SleepRecorder()

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"error": "busy"}, status=429)

    provider, _ = build_provider(
        {"llm": responder},
        sleep=sleeps,
        clock=SequenceClock([1000.0, 1000.0, 1025.0]),
    )

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "provider_timeout"
    assert sleeps.calls == []


async def test_text_pause_with_one_second_left_sleeps_within_budget_then_succeeds():
    sleeps = SleepRecorder()

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return json_response({"error": "busy"}, status=429)
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    provider, recorders = build_provider(
        {"llm": responder},
        sleep=sleeps,
        clock=SequenceClock([1000.0, 1000.0, 1024.0, 1024.0]),
    )

    assert isinstance(await provider.extract(extraction_context()), Extraction)
    assert len(recorders["llm"].requests) == 2
    assert len(sleeps.calls) == 1
    assert sleeps.calls[0] <= 1.0


@pytest.mark.parametrize("status,expected", [(599, "provider_unavailable"), (600, "provider_configuration")])
async def test_5xx_upper_boundary(status: int, expected: str):
    provider, recorders = build_provider({"llm": single_response("{}", status=status)})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == expected
    expected_attempts = 2 if expected == "provider_unavailable" else 1
    assert len(recorders["llm"].requests) == expected_attempts


async def test_base_url_ending_in_a_letter_is_not_stripped():
    profile = replace(LLM_PROFILE, base_url="http://llm.test/v1X")
    provider, recorders = build_provider(
        {"llm": extraction_responder()},
        profiles={"llm": profile, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": TTS_PROFILE},
    )

    await provider.extract(extraction_context())

    assert str(recorders["llm"].requests[0].url) == "http://llm.test/v1X/chat/completions"


async def test_stt_invalid_utf8_body_is_invalid_provider_response_after_attempts():
    provider, recorders = build_provider({"stt": single_response(b"\xff\xfe\x00")})

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["stt"].requests) == 2


async def test_stt_read_timeout_twice_is_provider_timeout():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider, recorders = build_provider({"stt": responder})

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "provider_timeout"
    assert len(recorders["stt"].requests) == 2


async def test_stt_zero_remaining_deadline_raises_timeout_without_a_wire_call():
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "hi"})},
        clock=SequenceClock([1000.0, 1035.0]),
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "provider_timeout"
    assert recorders["stt"].requests == []


async def test_stt_one_second_remaining_still_runs_the_attempt():
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "hi"})},
        clock=SequenceClock([1000.0, 1034.0]),
    )

    assert await provider.transcribe(b"wav") == "hi"
    assert len(recorders["stt"].requests) == 1


async def test_stt_multipart_request_is_a_post_with_exact_response_format():
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "hello"})}
    )

    await provider.transcribe(b"wav")

    request = recorders["stt"].requests[0]
    assert request.method == "POST"
    assert b'name="response_format"\r\n\r\njson\r\n' in request.content


async def test_tts_request_is_a_post():
    provider, recorders = build_provider({"tts": audio_responder(b"mp3")})

    await provider.synthesize("hello")

    assert recorders["tts"].requests[0].method == "POST"


async def test_tts_read_timeout_is_provider_timeout_with_one_attempt():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider, recorders = build_provider({"tts": responder})

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_timeout"
    assert len(recorders["tts"].requests) == 1


async def test_tts_zero_remaining_deadline_raises_timeout_without_a_wire_call():
    provider, recorders = build_provider(
        {"tts": audio_responder(b"mp3")}, clock=SequenceClock([1000.0, 1020.0])
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_timeout"
    assert recorders["tts"].requests == []


async def test_tts_one_second_remaining_still_runs_the_attempt():
    provider, recorders = build_provider(
        {"tts": audio_responder(b"mp3")}, clock=SequenceClock([1000.0, 1019.0])
    )

    assert (await provider.synthesize("hello")).data == b"mp3"
    assert len(recorders["tts"].requests) == 1


async def test_answerable_false_with_literal_xxxx_answer_is_invalid():
    bad = {"answerable": False, "answer": "XXXX", "cited_ids": []}
    provider, recorders = build_provider(
        {"rag": single_response(json.dumps(chat_envelope(json.dumps(bad))))}
    )

    with pytest.raises(AppError) as caught:
        await provider.answer_knowledge(knowledge_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["rag"].requests) == 2


@pytest.mark.parametrize("length,valid", [(1, True), (1200, True), (1201, False)])
async def test_answer_length_boundaries(length: int, valid: bool):
    answer = {"answerable": True, "answer": "a" * length, "cited_ids": ["faq-returns"]}
    provider, recorders = build_provider(
        {"rag": single_response(json.dumps(chat_envelope(json.dumps(answer))))}
    )

    if valid:
        result = await provider.answer_knowledge(knowledge_context())
        assert len(result.answer) == length
    else:
        with pytest.raises(AppError) as caught:
            await provider.answer_knowledge(knowledge_context())
        assert caught.value.code == "invalid_provider_response"
        assert len(recorders["rag"].requests) == 2


TWO_SOURCES = [
    SOURCE,
    KnowledgeRecord(
        id="faq-delivery",
        title="Delivery times",
        keywords=["delivery"],
        body="Standard delivery takes 2 to 5 business days after dispatch.",
    ),
]


def two_source_context() -> KnowledgeContext:
    return KnowledgeContext(
        question="Can I return an unused item?", kb_version="2026-10-22", sources=TWO_SOURCES
    )


@pytest.mark.parametrize(
    "cited_ids,valid",
    [([], False), (["faq-returns", "faq-delivery"], True), (["faq-returns", "faq-delivery", "faq-delivery"], False), (["faq-returns", "faq-returns"], False)],
)
async def test_cited_id_count_and_uniqueness_boundaries(cited_ids: list[str], valid: bool):
    answer = {"answerable": True, "answer": "Grounded answer.", "cited_ids": cited_ids}
    provider, recorders = build_provider(
        {"rag": single_response(json.dumps(chat_envelope(json.dumps(answer))))}
    )

    if valid:
        result = await provider.answer_knowledge(two_source_context())
        assert result.cited_ids == cited_ids
    else:
        with pytest.raises(AppError) as caught:
            await provider.answer_knowledge(two_source_context())
        assert caught.value.code == "invalid_provider_response"
        assert len(recorders["rag"].requests) == 2


async def test_multi_chunk_success_body_over_the_cap_is_rejected():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        stream = ChunkedStream([b"x" * 600000, b"x" * 448577])
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2


async def test_default_constructor_uses_real_sleep_for_jitter():
    import time as time_module

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"error": "busy"}, status=429)

    recorders = {role: RoleTransport(role, responder if role == "llm" else unexpected(role)) for role in ROLES}
    provider = OpenAICompatibleProvider(
        {"llm": LLM_PROFILE, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": TTS_PROFILE},
        transports={role: recorders[role].transport for role in ROLES},
    )
    started = time_module.monotonic()
    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())
    elapsed = time_module.monotonic() - started

    assert caught.value.code == "provider_unavailable"
    assert elapsed >= 0.2
    await provider.close()


async def test_default_transports_build_real_clients_that_close():
    provider = OpenAICompatibleProvider(
        {"llm": LLM_PROFILE, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": TTS_PROFILE}
    )

    assert set(provider._clients) == set(ROLES)
    await provider.close()
    assert all(client.is_closed for client in provider._clients.values())


async def test_client_timeouts_follow_each_roles_attempt_budget():
    provider, _ = build_provider({"llm": extraction_responder()})

    assert provider._clients["llm"].timeout.read == 10.0
    assert provider._clients["rag"].timeout.read == 10.0
    assert provider._clients["stt"].timeout.read == 15.0
    assert provider._clients["tts"].timeout.read == 20.0
    assert provider._clients["llm"].timeout.connect == 10.0
    await provider.close()


async def test_fifth_capacity_call_is_bounded_and_busy_without_queueing():
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

    tasks = [asyncio.create_task(provider.extract(extraction_context())) for _ in range(4)]
    await asyncio.wait_for(all_started.wait(), timeout=2)

    with pytest.raises(AppError) as caught:
        await asyncio.wait_for(provider.extract(extraction_context()), timeout=0.5)

    assert caught.value.code == "provider_busy"
    assert wire_calls == 4
    release.set()
    await asyncio.gather(*tasks)
    await provider.close()


async def test_released_permit_restores_capacity_one_at_a_time():
    held = asyncio.Event()
    release = asyncio.Event()
    wire_calls = 0

    async def blocking_responder(request: httpx.Request, attempt: int) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        await release.wait()
        await asyncio.sleep(0.01)
        return json_response(chat_envelope(json.dumps(EXTRACTION_PAYLOAD)))

    provider, _ = build_provider({"llm": blocking_responder}, capacity=1)
    task = asyncio.create_task(provider.extract(extraction_context()))
    await asyncio.sleep(0)
    with pytest.raises(AppError) as caught:
        await asyncio.wait_for(provider.extract(extraction_context()), timeout=0.5)
    assert caught.value.code == "provider_busy"

    release.set()
    await task

    results = await asyncio.gather(
        provider.extract(extraction_context()),
        provider.extract(extraction_context()),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, Extraction)]
    busy = [r for r in results if isinstance(r, AppError) and r.code == "provider_busy"]
    assert len(successes) == 1
    assert len(busy) == 1
    await provider.close()


# --------------------------------------------------------------------------
# Slice G: last-attempt deadline exhaustion and audio repair-line absence
# --------------------------------------------------------------------------


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
