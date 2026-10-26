"""IC §5 canonical intent table and mixed-intent rules at the real boundary.

Every test runs the real app factory, real lifespan, the real
`/api/sessions/{id}/turns` route, the real `ConversationEngine`, the real lexical
retriever over the bundled knowledge base and the real JSON repository in a
temporary directory. Only the external AI service is a deterministic ledger
(`ScriptedProvider`): E = one `extract` call, A = one `answer_knowledge` call.

Rows already owned by the intake (provide_details/correction), confirmation (reject/recovery/
review token), turn-limit, idempotency and RAG (supported/unsupported
questions, mixed facts+question, question at review) are covered by those
modules; this module adds the HTTP-level regression coverage for every remaining
table row and mixed-intent rule.

This file: mixed intents and guards."""

from __future__ import annotations

from uuid import uuid4

import pytest

from gateway.providers.prompts import (
    MISSING_CATEGORY,
    POLICY_QUESTION_SUFFIX,
    SOCIAL_ACKNOWLEDGMENT,
)
from tests.integration.support.api import make_extraction, read_session
from tests.integration.support.confirmation import (
    assert_error,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    post_confirm,
    review_session,
    send_turn,
)
from tests.integration.support.intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    REVIEW_SUFFIX,
    start_session,
    turn_body,
)
from tests.integration.support.intent import (
    PART_SLOTS,
    SOCIAL_GREETING,
    SOCIAL_THANKS,
    social_extraction,
)
from tests.integration.support.limits import raw_bytes


@pytest.mark.parametrize("has_question", [False, True])
async def test_social_preface_with_facts_starts_intake(
    client, provider, session_dir, has_question
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(
        make_extraction(
            intent="social",
            has_question=has_question,
            candidates={
                "order_number": "NS-000123",
                "description": "My order arrived broken",
            },
        )
    )
    customer_text = "Hello, my order NS-000123 arrived broken."

    request_id, response = await send_turn(
        client, session_id, text=customer_text, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # Real facts outrank the social preface: intake reply, never the acknowledgment.
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert SOCIAL_ACKNOWLEDGMENT not in payload["reply"]
    assert payload["reply"].startswith(MISSING_CATEGORY)
    assert (POLICY_QUESTION_SUFFIX in payload["reply"]) is has_question
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=customer_text,
        counter_delta=1,
    )


async def test_confirmation_with_a_policy_question_redisplays_the_review(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="confirm", has_question=True))

    request_id, response = await send_turn(
        client, session_id, text="Yes, can I return it?", revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == (
        f"{FULL_SUMMARY}{REVIEW_SUFFIX} {POLICY_QUESTION_SUFFIX}"
    )
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text="Yes, can I return it?",
        counter_delta=1,
    )
    assert disk["confirmation"] is None


async def test_completed_session_refuses_a_new_social_intent(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    _, confirmed = await post_confirm(
        client, session_id, expected_revision=1, review_revision=1
    )
    assert confirmed.status_code == 200, confirmed.text
    completed = read_session(session_dir, session_id)
    assert completed["status"] == "completed"
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=completed["revision"]
    )

    await assert_error(
        response,
        status=409,
        code="session_completed",
        message="This support conversation is complete. Start a new conversation if you need help with another issue.",
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes


async def test_revision_conflict_precedes_intent_routing(client, provider, session_dir):
    session_id = await start_session(client)
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await send_turn(client, session_id, text=SOCIAL_GREETING, revision=7)

    await assert_error(
        response,
        status=409,
        code="revision_conflict",
        message="This session changed. Refresh before sending again.",
        current_revision=0,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes


async def test_replay_of_a_social_turn_returns_the_stored_response(
    client, provider, session_dir
):
    session_id = await start_session(client)
    request_id = str(uuid4())
    body = turn_body(SOCIAL_GREETING, 0, request_id)
    provider.script_extraction(social_extraction(has_question=True))

    first = await client.post(f"/api/sessions/{session_id}/turns", json=body)
    assert first.status_code == 200, first.text
    original = first.json()
    calls_after_first = len(provider.calls)
    before_bytes = raw_bytes(session_dir, session_id)

    replay = await client.post(f"/api/sessions/{session_id}/turns", json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json() == original
    assert len(provider.calls) == calls_after_first
    assert raw_bytes(session_dir, session_id) == before_bytes


async def test_reused_request_id_with_a_changed_social_message_is_a_conflict(
    client, provider, session_dir
):
    session_id = await start_session(client)
    request_id = str(uuid4())
    provider.script_extraction(social_extraction(has_question=True))
    first = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(SOCIAL_GREETING, 0, request_id),
    )
    assert first.status_code == 200, first.text
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    conflict = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(SOCIAL_THANKS, 0, request_id),
    )

    await assert_error(
        conflict,
        status=409,
        code="request_id_conflict",
        message="This request ID was already used for different input.",
    )
    assert len(provider.calls) == calls_before
    assert raw_bytes(session_dir, session_id) == before_bytes
