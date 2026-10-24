"""Real RAG and application boundaries over a real provider HTTP service.

The app factory, lifespan, routes, schemas, JSON repository, conversation engine
and lexical retriever are the shipped implementations. No provider object is
injected: the lifespan constructs the real ``OpenAICompatibleProvider`` from
settings whose four roles point at ``LocalProviderServer`` on loopback, so every
extraction and answer crosses a genuine TCP/HTTP boundary.

``test_rag.py`` proves the same behaviours with a scripted provider boundary or
``httpx.MockTransport``. This module re-proves the ones that depend on the
provider transport through the real socket: grounded answers, no-match
abstention, unanswerable questions, two ordered sources, invalid citations,
generation outage rollback and the shared deadline. It also adds multi-turn and
reload, isolation and speech cases.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.conversation import RECENT_MESSAGE_LIMIT
from app.main import create_app
from app.prompts import ABSTENTION, EXTRACTION_SYSTEM_PROMPT, KNOWLEDGE_PROMPT
from app.providers import REPAIR_LINE
from tests.integration.support_api import running_client
from tests.integration.support_provider_http import (
    ABORTED,
    CHAT_PATH,
    SPEECH_PATH,
    LocalProviderServer,
    answer_payload,
    build_settings,
    chat_system_content,
    chat_user_content,
    extraction_payload,
)

KB = json.loads(
    (Path(__file__).resolve().parents[2] / "app" / "knowledge" / "northstar-faq.json").read_text(
        encoding="utf-8"
    )
)
KB_VERSION = KB["version"]
SEED_RECORDS = {record["id"]: record for record in KB["records"]}

RETURN_Q = "Can I return an unused item?"
PAYMENT_Q = "Which payment methods are accepted?"
REGION_Q = "Do you ship internationally?"
TWO_SOURCE_Q = "Can I return a damaged item and get a refund?"
DELIVERY_Q = "How long does delivery take?"
RETURN_ANSWER = (
    "You can request a return within 30 calendar days of delivery for an unused "
    "item in its original packaging."
)
TWO_SOURCE_ANSWER = (
    "Returns are requested within 30 calendar days and refund eligibility requires "
    "support review. For a damaged item, keep the packaging and describe the damage."
)
DELIVERY_ANSWER = "Standard delivery takes 2 to 5 business days after dispatch."


@pytest.fixture
def server():
    with LocalProviderServer() as active:
        yield active


def seed_citation(record_id: str) -> dict[str, Any]:
    record = SEED_RECORDS[record_id]
    return {
        "source_id": record_id,
        "title": record["title"],
        "excerpt": record["body"],
        "kb_version": KB_VERSION,
    }


def question_extraction() -> dict[str, Any]:
    return extraction_payload(intent="question", has_question=True)


def social_extraction() -> dict[str, Any]:
    return extraction_payload(intent="social", has_question=True)


def intake_extraction() -> dict[str, Any]:
    return extraction_payload(
        candidates={
            "order_number": "NS-000123",
            "description": "My order arrived broken",
        }
    )


def turn_body(text: str, revision: int) -> dict[str, str | int]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "text": text,
        "input_mode": "text",
    }


def persisted_bytes(session_dir: Path, session_id: str) -> bytes:
    return (session_dir / f"{session_id}.json").read_bytes()


def persisted(session_dir: Path, session_id: str) -> dict[str, Any]:
    return json.loads(persisted_bytes(session_dir, session_id))


async def start_session(client) -> str:
    response = await client.post("/api/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def post_turn(client, session_id: str, text: str, revision: int):
    return await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text, revision)
    )


def assert_error(response, *, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


# grounded answers over the real socket ---------------------


async def test_grounded_answer_crosses_real_http_and_commits(
    server, session_dir
):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        before = persisted(session_dir, session_id)
        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns"]))

        response = await post_turn(client, session_id, RETURN_Q, 0)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reply"] == RETURN_ANSWER
        assert body["citations"] == [seed_citation("faq-returns")]
        assert body["revision"] == 1
        assert body["status"] == "collecting"
        assert body["slots"] == before["slots"]

        llm_request, rag_request = server.requests(CHAT_PATH)
        assert chat_system_content(llm_request) == EXTRACTION_SYSTEM_PROMPT
        extraction_context = json.loads(chat_user_content(llm_request))
        assert extraction_context["current_message"] == RETURN_Q
        assert extraction_context["slots"] == before["slots"]
        assert chat_system_content(rag_request) == KNOWLEDGE_PROMPT
        assert json.loads(chat_user_content(rag_request)) == {
            "question": RETURN_Q,
            "kb_version": KB_VERSION,
            "sources": [SEED_RECORDS["faq-returns"]],
            "reply_language": "en",
        }

        disk = persisted(session_dir, session_id)
        assert disk["revision"] == 1
        assert disk["messages"][-2]["text"] == RETURN_Q
        assert disk["messages"][-2]["citations"] == []
        assert disk["messages"][-1]["id"] == body["message_id"]
        assert disk["messages"][-1]["text"] == body["reply"]
        assert disk["messages"][-1]["citations"] == body["citations"]
        assert disk["requests"][-1]["response"]["reply"] == RETURN_ANSWER


async def test_no_match_abstains_without_a_generation_call(server, session_dir):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(question_extraction())

        response = await post_turn(client, session_id, PAYMENT_Q, 0)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reply"] == ABSTENTION
        assert body["citations"] == []
        assert body["revision"] == 1
        # Exactly one request: the extraction. No retrieved record means the
        # engine never asks the RAG role to generate.
        assert server.paths() == [CHAT_PATH]
        assert chat_system_content(server.requests(CHAT_PATH)[0]) == EXTRACTION_SYSTEM_PROMPT
        disk = persisted(session_dir, session_id)
        assert disk["messages"][-1]["text"] == ABSTENTION
        assert disk["messages"][-1]["citations"] == []


async def test_retrieved_but_unanswerable_abstains(server, session_dir):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answerable=False))

        response = await post_turn(client, session_id, REGION_Q, 0)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reply"] == ABSTENTION
        assert body["citations"] == []
        assert body["revision"] == 1
        assert "internationally" not in body["reply"].lower()
        assert "region" not in body["reply"].lower()

        llm_request, rag_request = server.requests(CHAT_PATH)
        assert json.loads(chat_user_content(rag_request)) == {
            "question": REGION_Q,
            "kb_version": KB_VERSION,
            "sources": [SEED_RECORDS["faq-delivery"]],
            "reply_language": "en",
        }
        assert len(server.requests(CHAT_PATH)) == 2


async def test_two_sources_are_cited_in_retrieval_order(server, session_dir):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(question_extraction())
        server.script_chat(
            answer_payload(
                answer=TWO_SOURCE_ANSWER, cited_ids=["faq-returns", "faq-damaged"]
            )
        )

        response = await post_turn(client, session_id, TWO_SOURCE_Q, 0)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reply"] == TWO_SOURCE_ANSWER
        assert [citation["source_id"] for citation in body["citations"]] == [
            "faq-returns",
            "faq-damaged",
        ]
        assert body["citations"] == [
            seed_citation("faq-returns"),
            seed_citation("faq-damaged"),
        ]
        rag_context = json.loads(chat_user_content(server.requests(CHAT_PATH)[1]))
        assert [source["id"] for source in rag_context["sources"]] == [
            "faq-returns",
            "faq-damaged",
        ]
        disk = persisted(session_dir, session_id)
        assert disk["messages"][-1]["citations"] == body["citations"]


# invalid citations over the real socket --------------------

INVALID_ANSWERS = {
    "unknown_source": answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-unknown"]),
    "duplicate_source": answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns", "faq-returns"]),
    "unretrieved_source": answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-damaged"]),
    "unanswerable_with_text": answer_payload(answerable=False, answer="You can return it."),
}


@pytest.mark.parametrize("case_id", sorted(INVALID_ANSWERS))
async def test_invalid_citation_rolls_back(case_id, server, session_dir):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        before = persisted_bytes(session_dir, session_id)
        server.script_chat(question_extraction())
        server.script_chat(INVALID_ANSWERS[case_id])
        server.script_chat(INVALID_ANSWERS[case_id])

        response = await post_turn(client, session_id, RETURN_Q, 0)

        assert_error(response, status=502, code="invalid_provider_response")
        llm_requests = server.requests(CHAT_PATH)
        assert len(llm_requests) == 3
        rag_requests = llm_requests[1:]
        assert REPAIR_LINE not in chat_system_content(rag_requests[0])
        assert REPAIR_LINE in chat_system_content(rag_requests[1])
        assert persisted_bytes(session_dir, session_id) == before


# generation outage rolls back, session stays usable -----------------


async def test_generation_outage_rolls_back_then_recovers(server, session_dir):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        before = persisted_bytes(session_dir, session_id)
        server.script_chat(question_extraction())
        server.respond(CHAT_PATH, status=503, json_body={"error": "unavailable"})
        server.respond(CHAT_PATH, status=503, json_body={"error": "unavailable"})

        response = await post_turn(client, session_id, RETURN_Q, 0)

        assert_error(response, status=503, code="provider_unavailable")
        assert len(server.requests(CHAT_PATH)) == 3
        assert [request.status for request in server.requests(CHAT_PATH)] == [200, 503, 503]
        assert persisted_bytes(session_dir, session_id) == before

        # The same revision accepts a later turn: nothing was half-committed.
        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns"]))
        later = await post_turn(client, session_id, RETURN_Q, 0)
        assert later.status_code == 200, later.text
        assert later.json()["revision"] == 1
        assert later.json()["citations"] == [seed_citation("faq-returns")]


# one shared deadline across extraction and generation ---------------


async def test_shared_deadline_cuts_generation_without_detached_work(
    server, session_dir, monkeypatch
):
    # The engine owns the shared 25 s text-turn deadline. Shorten it for a cheap
    # wall-clock test; the extraction and answer both still cross real HTTP.
    import app.conversation as conversation

    monkeypatch.setattr(conversation, "TEXT_TURN_DEADLINE_SECONDS", 1.0)
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        before = persisted_bytes(session_dir, session_id)
        server.script_chat(question_extraction(), delay=0.1)
        server.respond(CHAT_PATH, json_body={}, delay=2.0)

        started = time.monotonic()
        response = await post_turn(client, session_id, RETURN_Q, 0)
        elapsed = time.monotonic() - started

        assert_error(response, status=504, code="provider_timeout")
        assert elapsed < 2.5, elapsed
        assert len(server.requests(CHAT_PATH)) == 2
        assert persisted_bytes(session_dir, session_id) == before

        # Let the delayed fake response finish, then confirm the timed-out turn
        # did not leave application work running or commit a late result.
        rag_requests = server.settle(CHAT_PATH, timeout=10.0)[1:]
        assert len(rag_requests) == 1
        time.sleep(0.2)
        assert len(server.requests(CHAT_PATH)) == 2
        assert persisted_bytes(session_dir, session_id) == before


# Multi-turn memory, reload and rollback --------------------------------------


async def test_multi_turn_rag_keeps_state_and_old_facts_outside_the_window(
    server, session_dir
):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)

        server.script_chat(intake_extraction())
        intake = await post_turn(
            client, session_id, "My order NS-000123 arrived broken.", 0
        )
        assert intake.status_code == 200, intake.text
        assert intake.json()["slots"]["order_number"] == "NS-000123"

        revision = intake.json()["revision"]
        for _ in range(6):
            server.script_chat(social_extraction())
            social = await post_turn(client, session_id, "Hello, how are you?", revision)
            assert social.status_code == 200, social.text
            revision = social.json()["revision"]
        assert revision == 7

        before_policy = persisted(session_dir, session_id)
        assert len(before_policy["messages"]) == 1 + 2 * 7
        window = before_policy["messages"][-RECENT_MESSAGE_LIMIT:]
        assert "NS-000123" not in [message["text"] for message in window]

        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns"]))
        policy = await post_turn(client, session_id, RETURN_Q, revision)
        assert policy.status_code == 200, policy.text
        policy_body = policy.json()
        assert policy_body["revision"] == 8
        assert policy_body["citations"] == [seed_citation("faq-returns")]
        assert policy_body["slots"]["order_number"] == "NS-000123"

        llm_request = server.requests(CHAT_PATH)[-2]
        context = json.loads(chat_user_content(llm_request))
        assert len(context["recent_messages"]) == RECENT_MESSAGE_LIMIT
        assert [message["text"] for message in context["recent_messages"]] == [
            message["text"] for message in window
        ]
        assert context["slots"]["order_number"] == "NS-000123"
        assert context["current_message"] == RETURN_Q

    # Refresh-equivalent reload: a fresh application instance reads the same
    # persisted session and serves the saved citations unchanged.
    reloaded_app = create_app(settings=settings)
    async with running_client(reloaded_app) as client:
        view = (await client.get(f"/api/sessions/{session_id}")).json()
        assert view["revision"] == 8
        assert view["slots"]["order_number"] == "NS-000123"
        saved = [message for message in view["messages"] if message["citations"]]
        assert [message["citations"] for message in saved] == [
            [seed_citation("faq-returns")]
        ]

        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=DELIVERY_ANSWER, cited_ids=["faq-delivery"]))
        fresh = await post_turn(client, session_id, DELIVERY_Q, 8)
        assert fresh.status_code == 200, fresh.text
        fresh_body = fresh.json()
        assert fresh_body["revision"] == 9
        assert fresh_body["citations"] == [seed_citation("faq-delivery")]
        assert fresh_body["slots"]["order_number"] == "NS-000123"

        extraction_context = json.loads(
            chat_user_content(server.requests(CHAT_PATH)[-2])
        )
        assert len(extraction_context["recent_messages"]) == RECENT_MESSAGE_LIMIT
        assert extraction_context["slots"]["order_number"] == "NS-000123"

        disk = persisted(session_dir, session_id)
        assert disk["revision"] == 9
        assert disk["slots"]["order_number"] == "NS-000123"
        assert [message["citations"] for message in disk["messages"] if message["citations"]] == [
            [seed_citation("faq-returns")],
            [seed_citation("faq-delivery")],
        ]


async def test_rollback_after_reload_leaves_the_persisted_session_untouched(
    server, session_dir
):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns"]))
        first = await post_turn(client, session_id, RETURN_Q, 0)
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1

    reloaded_app = create_app(settings=settings)
    async with running_client(reloaded_app) as client:
        view = (await client.get(f"/api/sessions/{session_id}")).json()
        assert view["revision"] == 1
        before = persisted_bytes(session_dir, session_id)

        server.script_chat(question_extraction())
        server.respond(CHAT_PATH, status=503, json_body={"error": "unavailable"})
        server.respond(CHAT_PATH, status=503, json_body={"error": "unavailable"})
        outage = await post_turn(client, session_id, DELIVERY_Q, 1)

        assert_error(outage, status=503, code="provider_unavailable")
        assert persisted_bytes(session_dir, session_id) == before
        unchanged = (await client.get(f"/api/sessions/{session_id}")).json()
        assert unchanged["revision"] == 1
        assert unchanged["messages"] == view["messages"]

        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=DELIVERY_ANSWER, cited_ids=["faq-delivery"]))
        recovered = await post_turn(client, session_id, DELIVERY_Q, 1)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["revision"] == 2
        assert recovered.json()["citations"] == [seed_citation("faq-delivery")]


# Isolation -------------------------------------------------------------------


async def test_two_app_instances_and_sessions_are_isolated(tmp_path):
    with LocalProviderServer() as server_a, LocalProviderServer() as server_b:
        settings_a = build_settings(tmp_path / "a" / "sessions", server_a)
        settings_b = build_settings(tmp_path / "b" / "sessions", server_b)
        app_a = create_app(settings=settings_a)
        app_b = create_app(settings=settings_b)

        async with running_client(app_a) as client_a, running_client(app_b) as client_b:
            session_a = await start_session(client_a)
            session_b = await start_session(client_b)
            assert session_a != session_b

            server_a.script_chat(question_extraction())
            server_a.script_chat(answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns"]))
            server_b.script_chat(question_extraction())
            server_b.script_chat(answer_payload(answer=DELIVERY_ANSWER, cited_ids=["faq-delivery"]))

            turn_a = await post_turn(client_a, session_a, RETURN_Q, 0)
            turn_b = await post_turn(client_b, session_b, DELIVERY_Q, 0)

            assert turn_a.status_code == turn_b.status_code == 200
            assert turn_a.json()["citations"] == [seed_citation("faq-returns")]
            assert turn_b.json()["citations"] == [seed_citation("faq-delivery")]
            assert server_a.count(CHAT_PATH) == 2
            assert server_b.count(CHAT_PATH) == 2

            # A second session in the same application has no shared state.
            session_a2 = await start_session(client_a)
            view_a2 = (await client_a.get(f"/api/sessions/{session_a2}")).json()
            assert view_a2["revision"] == 0
            assert view_a2["slots"]["order_number"] is None
            assert len(view_a2["messages"]) == 1

            disk_a = persisted(tmp_path / "a" / "sessions", session_a)
            disk_b = persisted(tmp_path / "b" / "sessions", session_b)
            assert disk_a["messages"][-1]["citations"] == [seed_citation("faq-returns")]
            assert disk_b["messages"][-1]["citations"] == [seed_citation("faq-delivery")]
            assert disk_a["session_id"] != disk_b["session_id"]


# Speech route over real HTTP -------------------------------------------------


async def test_speech_synthesizes_only_a_saved_message_over_real_http(
    server, session_dir
):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(intake_extraction())
        turn = await post_turn(client, session_id, "My order NS-000123 arrived broken.", 0)
        assert turn.status_code == 200, turn.text
        reply = turn.json()["reply"]
        message_id = turn.json()["message_id"]
        before = persisted_bytes(session_dir, session_id)

        audio = b"ID3\x03\x00\x00\x00\x00\x00\x00mp3-fixture"
        server.respond(
            SPEECH_PATH,
            body=audio,
            headers={"content-type": "audio/mpeg"},
        )
        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": message_id}
        )

        assert response.status_code == 200, response.text
        assert response.content == audio
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert persisted_bytes(session_dir, session_id) == before

        speech_requests = server.requests(SPEECH_PATH)
        assert len(speech_requests) == 1
        assert speech_requests[0].header("authorization") is None
        assert speech_requests[0].json() == {
            "model": "tts-model",
            "voice": "test-voice",
            "input": reply,
            "response_format": "mp3",
        }


async def test_speech_rejects_unsaved_or_arbitrary_input_without_provider_call(
    server, session_dir
):
    settings = build_settings(session_dir, server)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(intake_extraction())
        turn = await post_turn(client, session_id, "My order NS-000123 arrived broken.", 0)
        assert turn.status_code == 200, turn.text
        view = (await client.get(f"/api/sessions/{session_id}")).json()
        user_message = next(m for m in view["messages"] if m["role"] == "user")
        before = persisted_bytes(session_dir, session_id)

        unknown = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": str(uuid4())}
        )
        assert_error(unknown, status=404, code="message_not_found")

        user_turn = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": user_message["id"]}
        )
        assert_error(user_turn, status=404, code="message_not_found")

        arbitrary_text = await client.post(
            f"/api/sessions/{session_id}/speech",
            json={"message_id": turn.json()["message_id"], "text": "Say anything."},
        )
        assert_error(arbitrary_text, status=422, code="invalid_request")

        assert server.requests(SPEECH_PATH) == []
        assert persisted_bytes(session_dir, session_id) == before


# Output modes at the application boundary ------------------------------------


@pytest.mark.parametrize("output_mode", ["json_schema", "json_object", "prompt_json"])
async def test_output_mode_drives_a_real_turn_end_to_end(output_mode, server, session_dir):
    settings = build_settings(session_dir, server, output_mode=output_mode)
    app = create_app(settings=settings)
    async with running_client(app) as client:
        session_id = await start_session(client)
        server.script_chat(question_extraction())
        server.script_chat(answer_payload(answer=RETURN_ANSWER, cited_ids=["faq-returns"]))

        response = await post_turn(client, session_id, RETURN_Q, 0)

        assert response.status_code == 200, response.text
        assert response.json()["citations"] == [seed_citation("faq-returns")]
        llm_payload = server.requests(CHAT_PATH)[0].json()
        rag_payload = server.requests(CHAT_PATH)[1].json()
        if output_mode == "json_schema":
            assert llm_payload["response_format"]["json_schema"]["name"] == "extraction"
            assert rag_payload["response_format"]["json_schema"]["name"] == "knowledge_answer"
        elif output_mode == "json_object":
            assert llm_payload["response_format"] == {"type": "json_object"}
            assert rag_payload["response_format"] == {"type": "json_object"}
        else:
            assert "response_format" not in llm_payload
            assert "response_format" not in rag_payload
            assert "Return one JSON object matching this schema" in chat_system_content(
                server.requests(CHAT_PATH)[0]
            )
