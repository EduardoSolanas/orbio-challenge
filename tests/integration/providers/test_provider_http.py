"""Real provider HTTP adapter against a real loopback service.

Every case here drives the shipped ``OpenAICompatibleProvider`` with its real
``httpx.AsyncClient`` over a real TCP socket to ``LocalProviderServer``. Unit
tests and the existing transcription, transport and bounds cases double the transport with
``httpx.MockTransport``; these cases prove the wire-level behaviour those doubles
cannot: actual request framing, redirect non-following, chunked body bounds with
an observable client abort, encoded-response rejection and read timeouts with no
detached provider work.
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from gateway.errors import AppError
from gateway.providers.client import (
    REPAIR_LINE,
    SCHEMA_INSTRUCTION,
    SUCCESS_JSON_LIMIT,
    OpenAICompatibleProvider,
)
from gateway.providers.prompts import EXTRACTION_SYSTEM_PROMPT, KNOWLEDGE_PROMPT
from gateway.schemas import (
    Extraction,
    KnowledgeAnswer,
    KnowledgeRecord,
)
from tests.integration.support.adapter import extraction_context
from tests.integration.support.provider_http import (
    ABORTED,
    CHAT_PATH,
    REDIRECTED,
    answer_payload,
    build_settings,
    chat_system_content,
    chat_user_content,
    extraction_payload,
    provider_from,
    shorten_text_budget,
)
from tests.unit.gateway.providers.provider_support import knowledge_context

SOURCE = KnowledgeRecord(
    id="faq-returns",
    title="Returns and refunds",
    keywords=["return", "refund"],
    body="An unused item can be requested for return within 30 calendar days.",
)
SECOND_SOURCE = KnowledgeRecord(
    id="faq-damaged",
    title="Damaged items",
    keywords=["damaged", "broken"],
    body="Keep the packaging and describe the damage for a damaged item.",
)

EXTRACTION = extraction_payload(
    candidates={"order_number": "NS-000123", "description": "My order arrived broken"}
)
ANSWER = answer_payload(
    answer="You can request a return within 30 calendar days of delivery.",
    cited_ids=["faq-returns"],
)




async def no_sleep(delay: float) -> None:
    """Injected retry pause: no wall-clock jitter in transport tests."""




# Exact requests and parsed results -------------------------------------------


async def test_extraction_and_answer_requests_cross_the_real_socket_exactly(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    server.script_chat(EXTRACTION)
    server.script_chat(ANSWER)
    provider = provider_from(settings)
    try:
        extraction = await provider.extract(extraction_context())
        answer = await provider.answer_knowledge(knowledge_context())
    finally:
        await provider.close()

    assert extraction == Extraction.model_validate(EXTRACTION)
    assert answer == KnowledgeAnswer.model_validate(ANSWER)
    assert server.paths() == [CHAT_PATH, CHAT_PATH]

    llm_request, rag_request = server.requests(CHAT_PATH)
    assert llm_request.outcome == "completed"
    assert llm_request.header("authorization") == "Bearer llm-secret"
    assert llm_request.header("accept-encoding") == "identity"
    assert llm_request.header("content-type") == "application/json"
    assert llm_request.json() == {
        "model": "llm-model",
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    extraction_context().model_dump(mode="json"), ensure_ascii=False
                ),
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

    assert rag_request.header("authorization") == "Bearer rag-secret"
    assert rag_request.json() == {
        "model": "rag-model",
        "messages": [
            {"role": "system", "content": KNOWLEDGE_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    knowledge_context().model_dump(mode="json"), ensure_ascii=False
                ),
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


async def test_role_tokens_never_cross_the_wire(server, tmp_path):
    settings = build_settings(
        tmp_path / "sessions", server, llm_key="llm-only", rag_key="rag-only"
    )
    server.script_chat(EXTRACTION)
    server.script_chat(ANSWER)
    provider = provider_from(settings)
    try:
        await provider.extract(extraction_context())
        await provider.answer_knowledge(knowledge_context())
    finally:
        await provider.close()

    llm_request, rag_request = server.requests(CHAT_PATH)
    assert llm_request.header("authorization") == "Bearer llm-only"
    assert rag_request.header("authorization") == "Bearer rag-only"
    assert "rag-only" not in json.dumps(llm_request.headers)
    assert "llm-only" not in json.dumps(rag_request.headers)


@pytest.mark.parametrize("output_mode", ["json_schema", "json_object", "prompt_json"])
async def test_all_three_output_modes_are_transported_on_the_wire(
    output_mode, server, tmp_path
):
    settings = build_settings(tmp_path / "sessions", server, output_mode=output_mode)
    server.script_chat(EXTRACTION)
    server.script_chat(ANSWER)
    provider = provider_from(settings)
    try:
        await provider.extract(extraction_context())
        await provider.answer_knowledge(knowledge_context())
    finally:
        await provider.close()

    llm_request, rag_request = server.requests(CHAT_PATH)
    for request, schema, name in (
        (llm_request, Extraction, "extraction"),
        (rag_request, KnowledgeAnswer, "knowledge_answer"),
    ):
        payload = request.json()
        system = chat_system_content(request)
        if output_mode == "json_schema":
            assert payload["response_format"] == {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
            assert system in (EXTRACTION_SYSTEM_PROMPT, KNOWLEDGE_PROMPT)
        elif output_mode == "json_object":
            assert payload["response_format"] == {"type": "json_object"}
            assert system.endswith(
                SCHEMA_INSTRUCTION + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )
        else:
            assert "response_format" not in payload
            assert system.endswith(
                SCHEMA_INSTRUCTION + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )

    assert chat_user_content(llm_request) == json.dumps(
        extraction_context().model_dump(mode="json"), ensure_ascii=False
    )
    assert chat_user_content(rag_request) == json.dumps(
        knowledge_context().model_dump(mode="json"), ensure_ascii=False
    )


# Transport failures ----------------------------------------------------------


async def test_redirect_is_not_followed(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    server.respond(
        CHAT_PATH,
        status=302,
        location=server.base_url + "/redirected",
    )
    provider = provider_from(settings)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "provider_configuration"
    assert excinfo.value.status == 503
    assert server.paths() == [CHAT_PATH]
    assert server.requests(CHAT_PATH)[0].outcome == REDIRECTED
    assert server.count("/v1/redirected") == 0


async def test_success_body_exactly_at_the_limit_is_accepted(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    envelope = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(EXTRACTION)}}
        ],
        "pad": "",
    }
    base = json.dumps(envelope).encode("utf-8")
    envelope["pad"] = "x" * (SUCCESS_JSON_LIMIT - len(base))
    body = json.dumps(envelope).encode("utf-8")
    assert len(body) == SUCCESS_JSON_LIMIT
    server.respond(CHAT_PATH, chunks=[body[i : i + 65536] for i in range(0, len(body), 65536)])

    provider = provider_from(settings)
    try:
        extraction = await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert extraction == Extraction.model_validate(EXTRACTION)
    assert server.count(CHAT_PATH) == 1
    assert server.requests(CHAT_PATH)[0].bytes_written == SUCCESS_JSON_LIMIT


async def test_oversized_chunked_success_is_bounded_and_retried(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    chunk = b"x" * 16384
    chunks = [chunk] * 512  # 8 MiB: far beyond the 1 MiB raw success cap.
    total = sum(len(part) for part in chunks)
    server.respond(CHAT_PATH, chunks=chunks)
    server.respond(CHAT_PATH, chunks=chunks)

    provider = provider_from(settings)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "invalid_provider_response"
    assert excinfo.value.status == 502
    requests = server.settle(CHAT_PATH)
    assert len(requests) == 2
    # The client aborted each real stream after the cap; the server never wrote
    # the whole 8 MiB, which is what bounded consumption means at the socket.
    assert all(request.outcome == ABORTED for request in requests)
    assert all(request.bytes_written < total for request in requests)


async def test_gzip_encoded_success_is_rejected_without_decompression(server, tmp_path):
    import gzip

    settings = build_settings(tmp_path / "sessions", server)
    plain = json.dumps(
        {
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(EXTRACTION)}}
            ]
        }
    ).encode("utf-8")
    compressed = gzip.compress(plain)
    server.respond(
        CHAT_PATH,
        body=compressed,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )
    server.respond(
        CHAT_PATH,
        body=compressed,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )

    provider = provider_from(settings)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "invalid_provider_response"
    requests = server.requests(CHAT_PATH)
    assert len(requests) == 2
    assert all(request.status == 200 for request in requests)
    assert all(
        request.response_headers.get("content-encoding") == "gzip"
        for request in requests
    )
    assert REPAIR_LINE in chat_system_content(requests[1])
    assert REPAIR_LINE not in chat_system_content(requests[0])


async def test_truncated_success_is_invalid_and_repaired_once(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    server.script_chat(EXTRACTION, finish_reason="length")
    server.script_chat(EXTRACTION, finish_reason="length")

    provider = provider_from(settings)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "invalid_provider_response"
    requests = server.requests(CHAT_PATH)
    assert len(requests) == 2
    assert REPAIR_LINE not in chat_system_content(requests[0])
    assert REPAIR_LINE in chat_system_content(requests[1])


async def test_provider_503_is_retried_then_unavailable(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    server.respond(CHAT_PATH, status=503, json_body={"error": "unavailable"})
    server.respond(CHAT_PATH, status=503, json_body={"error": "unavailable"})

    provider = provider_from(settings, sleep=no_sleep)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "provider_unavailable"
    assert excinfo.value.status == 503
    assert server.count(CHAT_PATH) == 2


async def test_provider_400_is_not_retried_and_maps_to_configuration(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    server.respond(CHAT_PATH, status=400, json_body={"error": "denied"})

    provider = provider_from(settings, sleep=no_sleep)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "provider_configuration"
    assert excinfo.value.status == 503
    assert excinfo.value.retryable is False
    assert server.count(CHAT_PATH) == 1


async def extract_against_a_slow_server(server, tmp_path, monkeypatch) -> tuple[AppError, float]:
    """Two attempts, each answered a second late, against a 0.3 s attempt budget."""

    shorten_text_budget(monkeypatch, attempt_timeout=0.3, total_deadline=5.0)
    settings = build_settings(tmp_path / "sessions", server)
    server.respond(CHAT_PATH, json_body={}, delay=1.0)
    server.respond(CHAT_PATH, json_body={}, delay=1.0)

    provider = provider_from(settings)
    try:
        started = time.monotonic()
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
        elapsed = time.monotonic() - started
    finally:
        await provider.close()
    return excinfo.value, elapsed


async def test_read_timeout_is_bounded(server, tmp_path, monkeypatch):
    error, elapsed = await extract_against_a_slow_server(server, tmp_path, monkeypatch)

    assert error.code == "provider_timeout"
    assert error.status == 504
    assert elapsed < 2.5, elapsed


async def test_a_timed_out_call_makes_only_the_configured_attempts_and_leaves_no_detached_work(
    server, tmp_path, monkeypatch
):
    await extract_against_a_slow_server(server, tmp_path, monkeypatch)

    # Wait for both attempts to reach the server, then for the delayed responses to finish.
    requests = server.settle(CHAT_PATH, expected=2)

    assert len(requests) == 2
    # Completing the delayed fake responses must not start another provider attempt.
    assert server.count(CHAT_PATH) == 2


async def test_connection_refused_maps_to_unavailable(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = build_settings(
        tmp_path / "sessions",
        type("Unused", (), {"base_url": f"http://127.0.0.1:{port}/v1"})(),
        auth="none",
    )

    provider: OpenAICompatibleProvider = provider_from(settings, sleep=no_sleep)
    try:
        with pytest.raises(AppError) as excinfo:
            await provider.extract(extraction_context())
    finally:
        await provider.close()

    assert excinfo.value.code == "provider_unavailable"
    assert excinfo.value.status == 503
