"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic: failure edges."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from gateway.errors import AppError
from gateway.providers.client import OpenAICompatibleProvider
from gateway.schemas import Extraction, KnowledgeContext, KnowledgeRecord
from tests.unit.gateway.providers.provider_support import (
    EXTRACTION_PAYLOAD,
    LLM_PROFILE,
    RAG_PROFILE,
    ROLES,
    SOURCE,
    STT_PROFILE,
    TTS_PROFILE,
    ChunkedStream,
    RoleTransport,
    SequenceClock,
    SleepRecorder,
    SlowStream,
    audio_responder,
    build_provider,
    chat_envelope,
    extraction_context,
    extraction_responder,
    json_response,
    knowledge_context,
    single_response,
    unexpected,
)


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


async def test_text_asyncio_timeout_then_success_pauses_and_retries():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(
                200,
                stream=SlowStream(
                    0.3,
                    json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))).encode(),
                ),
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
        responder = single_response(
            json.dumps(chat_envelope(json.dumps(EXTRACTION_PAYLOAD))), status=300
        )
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


@pytest.mark.parametrize(
    "status,expected", [(599, "provider_unavailable"), (600, "provider_configuration")]
)
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
        profiles={
            "llm": profile,
            "rag": RAG_PROFILE,
            "stt": STT_PROFILE,
            "tts": TTS_PROFILE,
        },
    )

    await provider.extract(extraction_context())

    assert (
        str(recorders["llm"].requests[0].url) == "http://llm.test/v1X/chat/completions"
    )


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
        question="Can I return an unused item?",
        kb_version="2026-10-22",
        sources=TWO_SOURCES,
    )


@pytest.mark.parametrize(
    "cited_ids,valid",
    [
        ([], False),
        (["faq-returns", "faq-delivery"], True),
        (["faq-returns", "faq-delivery", "faq-delivery"], False),
        (["faq-returns", "faq-returns"], False),
    ],
)
async def test_cited_id_count_and_uniqueness_boundaries(
    cited_ids: list[str], valid: bool
):
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
        return httpx.Response(
            200, stream=stream, headers={"content-type": "application/json"}
        )

    provider, recorders = build_provider({"llm": responder})

    with pytest.raises(AppError) as caught:
        await provider.extract(extraction_context())

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["llm"].requests) == 2


async def test_default_constructor_uses_real_sleep_for_jitter():
    import time as time_module

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({"error": "busy"}, status=429)

    recorders = {
        role: RoleTransport(role, responder if role == "llm" else unexpected(role))
        for role in ROLES
    }
    provider = OpenAICompatibleProvider(
        {
            "llm": LLM_PROFILE,
            "rag": RAG_PROFILE,
            "stt": STT_PROFILE,
            "tts": TTS_PROFILE,
        },
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

    tasks = [
        asyncio.create_task(provider.extract(extraction_context())) for _ in range(4)
    ]
    await asyncio.wait_for(all_started.wait(), timeout=2)

    with pytest.raises(AppError) as caught:
        await asyncio.wait_for(provider.extract(extraction_context()), timeout=0.5)

    assert caught.value.code == "provider_busy"
    assert wire_calls == 4
    release.set()
    await asyncio.gather(*tasks)
    await provider.close()


async def test_released_permit_restores_capacity_one_at_a_time():
    release = asyncio.Event()
    wire_calls = 0

    async def blocking_responder(
        request: httpx.Request, attempt: int
    ) -> httpx.Response:
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
