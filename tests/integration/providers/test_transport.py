"""Four-role transport, auth, output modes and status mapping.

Every case resolves a real `Settings` from a complete isolated environment, runs
the real app lifespan and exercises the shipped `OpenAICompatibleProvider`
against one real `httpx.AsyncClient` per role wired to a per-role
`httpx.MockTransport` (IC §13.1, §13.2; PC §3). Only the external service is
doubled; URL joining, per-role headers, request serialization, strict parsing,
retry/repair budgets and status-derived error mapping are production code.

No credential or developer environment value is read: each case passes an
explicit mapping to `Settings.from_env`.
"""

from __future__ import annotations

import inspect
import json
from functools import partial

import httpx
import pytest

from gateway.config import Settings
from gateway.main import create_app
from gateway.providers.prompts import EXTRACTION_SYSTEM_PROMPT, KNOWLEDGE_PROMPT, serialize_context
from gateway.providers.client import REPAIR_LINE, SCHEMA_INSTRUCTION
from gateway.schemas import Extraction, KnowledgeAnswer, KnowledgeContext, KnowledgeRecord
from tests.integration.support.adapter import (
    EXTRACTION_PAYLOAD,
    ROLES,
    SleepRecorder,
    adapter_app,
    apply_env,
    assert_app_error,
    chat_envelope,
    chat_valid,
    env,
    extraction_context,
    settings_from,
)
from tests.integration.support.intake import start_session, turn_body
from tests.integration.support.transcription import (
    assert_error,
    json_wire,
    snapshot,
    streamed_wire,
)


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


def knowledge_context() -> KnowledgeContext:
    return KnowledgeContext(
        question="Can I return an unused item?",
        kb_version="2026-10-22",
        sources=[SOURCE],
    )


def status_responder(status: int, body: bytes = b'{"error":"denied"}', headers=None):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_wire([body], status=status, headers=headers)[0]

    return responder


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.index = 0

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def expect_error(error: BaseException, *, status: int, code: str) -> None:
    assert_app_error(error, status=status, code=code)


SENTIMENT_ENUM = ["positive", "neutral", "negative", "uncertain"]
FRUSTRATION_ENUM = ["none", "present", "uncertain"]


def assert_emotion_schema(schema: dict) -> None:
    """The extended extraction schema carries both required emotion labels."""

    assert schema["properties"]["sentiment"]["enum"] == SENTIMENT_ENUM
    assert schema["properties"]["frustration"]["enum"] == FRUSTRATION_ENUM
    assert {"sentiment", "frustration"} <= set(schema["required"])


# llm rag json schema output mode..llm rag prompt json output mode: output modes ---------------------------------------------


async def case_llm_rag_json_schema_output_mode(session_dir):
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


async def case_llm_rag_json_object_output_mode(session_dir):
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


async def case_llm_rag_prompt_json_output_mode(session_dir):
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


# four independent service endpoints..distinct bearer tokens on four roles: four independent roles, auth and RAG inheritance ----------


async def case_four_independent_service_endpoints(session_dir):
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


async def case_one_shared_host_four_explicit_role_profiles(session_dir):
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


async def case_all_empty_rag_profile_real_settings_lifespan_resolution_then(session_dir):
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


async def case_partially_specified_rag_profile(session_dir, monkeypatch):
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


async def case_auth_mode_none_on_all_roles(session_dir):
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


async def case_distinct_bearer_tokens_on_four_roles(session_dir):
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


# http redirect..http500 twice text stt: redirect and HTTP status mapping -------------------------


async def case_http_redirect(session_dir):
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


async def case_configuration_status(status, session_dir, caplog):
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


async def case_http429_twice_text_stt(session_dir):
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


async def case_http500_twice_text_stt(session_dir):
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


# 2xx malformed json twice..2xx content list multipart blocks on both attempts: unusable 2xx payloads, bounded attempts, no commit --------


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


async def case_2xx_malformed_json_twice(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "malformed")


async def case_2xx_response_choices_on_both_attempts(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "empty_choices"
    )


async def case_2xx_message_tool_calls_nonempty_twice(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "tool_calls")


async def case_2xx_refusal_nonempty_twice(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "refusal")


async def case_2xx_finish_reason_length_on_both_attempts(session_dir):
    await assert_invalid_success(settings_from(env(session_dir)), session_dir, "length")


async def case_2xx_message_content_null_on_both_attempts(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "null_content"
    )


async def case_2xx_response_missing_choices_key_on_both_attempts(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "missing_choices"
    )


async def case_2xx_finish_reason_null_on_both_attempts(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "null_finish_reason"
    )


async def case_2xx_content_list_multipart_blocks_on_both_attempts(session_dir):
    await assert_invalid_success(
        settings_from(env(session_dir)), session_dir, "multipart_content"
    )


CASES = {
    "llm_rag_json_schema_output_mode": case_llm_rag_json_schema_output_mode,
    "llm_rag_json_object_output_mode": case_llm_rag_json_object_output_mode,
    "llm_rag_prompt_json_output_mode": case_llm_rag_prompt_json_output_mode,
    "four_independent_service_endpoints": case_four_independent_service_endpoints,
    "one_shared_host_four_explicit_role_profiles": case_one_shared_host_four_explicit_role_profiles,
    "all_empty_rag_profile_real_settings_lifespan_resolution_then": case_all_empty_rag_profile_real_settings_lifespan_resolution_then,
    "partially_specified_rag_profile": case_partially_specified_rag_profile,
    "auth_mode_none_on_all_roles": case_auth_mode_none_on_all_roles,
    "distinct_bearer_tokens_on_four_roles": case_distinct_bearer_tokens_on_four_roles,
    "http_redirect": case_http_redirect,
    "http400": partial(case_configuration_status, 400),
    "http401": partial(case_configuration_status, 401),
    "http403": partial(case_configuration_status, 403),
    "http404_model_endpoint_absent": partial(case_configuration_status, 404),
    "http405": partial(case_configuration_status, 405),
    "http415": partial(case_configuration_status, 415),
    "http422": partial(case_configuration_status, 422),
    "http429_twice_text_stt": case_http429_twice_text_stt,
    "http500_twice_text_stt": case_http500_twice_text_stt,
    "2xx_malformed_json_twice": case_2xx_malformed_json_twice,
    "2xx_response_choices_on_both_attempts": case_2xx_response_choices_on_both_attempts,
    "2xx_message_tool_calls_nonempty_twice": case_2xx_message_tool_calls_nonempty_twice,
    "2xx_refusal_nonempty_twice": case_2xx_refusal_nonempty_twice,
    "2xx_finish_reason_length_on_both_attempts": case_2xx_finish_reason_length_on_both_attempts,
    "2xx_message_content_null_on_both_attempts": case_2xx_message_content_null_on_both_attempts,
    "2xx_response_missing_choices_key_on_both_attempts": case_2xx_response_missing_choices_key_on_both_attempts,
    "2xx_finish_reason_null_on_both_attempts": case_2xx_finish_reason_null_on_both_attempts,
    "2xx_content_list_multipart_blocks_on_both_attempts": case_2xx_content_list_multipart_blocks_on_both_attempts,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_transport(case_id, session_dir, monkeypatch, caplog):
    case = CASES[case_id]
    kwargs = {"session_dir": session_dir}
    parameters = inspect.signature(case).parameters
    if "monkeypatch" in parameters:
        kwargs["monkeypatch"] = monkeypatch
    if "caplog" in parameters:
        kwargs["caplog"] = caplog
    await case(**kwargs)
