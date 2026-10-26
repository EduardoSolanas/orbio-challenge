"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic: transport."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from gateway.errors import AppError
from gateway.schemas import Extraction
from tests.unit.gateway.providers.provider_support import (
    ANSWER_PAYLOAD,
    EXTRACTION_PAYLOAD,
    LLM_PROFILE,
    RAG_PROFILE,
    ROLES,
    STT_PROFILE,
    TTS_PROFILE,
    Clock,
    SleepRecorder,
    build_provider,
    chat_envelope,
    extraction_context,
    extraction_responder,
    json_response,
    knowledge_context,
    streamed_response,
)


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
