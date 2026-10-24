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

This file: social and gratitude turns."""

from __future__ import annotations

import pytest

from app.prompts import (
    ABSTENTION,
    INVALID_ORDER,
    MISSING_CATEGORY,
    REJECT_PROMPT,
    GRATITUDE_ACKNOWLEDGMENT,
    SOCIAL_ACKNOWLEDGMENT,
)
from tests.integration.confirmation_support import (
    assert_error,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    rejected_session,
    review_session,
    send_turn,
)
from tests.integration.intent_support import (
    CAPABILITY_Q,
    PART_SLOTS,
    SOCIAL_GREETING,
    SOCIAL_THANKS,
    START_TEXT,
    UNRELATED_TEXT,
    assert_no_policy_suffix,
    social_extraction,
)
from tests.integration.support_api import make_extraction, read_session
from tests.integration.test_intake import (
    EMPTY_SLOTS,
    FULL_SLOTS,
    FULL_SUMMARY,
    PART_TEXT,
    REVIEW_SUFFIX,
    part_extraction,
    post_turn,
    start_session,
)
from tests.integration.test_turn_limits import (
    AT_CAP,
    PARTIAL_SLOTS,
    TURN_LIMIT_MESSAGE,
    raw_bytes,
    seed_session,
)


def gratitude_extraction():
    return make_extraction(intent="gratitude", sentiment="positive")


async def test_social_greeting_returns_only_the_exact_acknowledgment(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert ABSTENTION not in payload["reply"]
    assert_no_policy_suffix(payload["reply"])
    assert payload["citations"] == []
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_GREETING,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False


async def test_social_thanks_preserves_accepted_facts_and_pending_state(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, part = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    assert part["reply"] == MISSING_CATEGORY
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(gratitude_extraction())

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_THANKS, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == GRATITUDE_ACKNOWLEDGMENT
    assert MISSING_CATEGORY not in payload["reply"]
    assert_no_policy_suffix(payload["reply"])
    assert_session_state(
        payload,
        revision=part["revision"] + 1,
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
        text=SOCIAL_THANKS,
        counter_delta=1,
    )


async def test_social_at_a_valid_review_preserves_the_token_summary_and_status(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    assert before["pending_review_revision"] == 1
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert REVIEW_SUFFIX not in payload["reply"]
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
        text=SOCIAL_GREETING,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["pending_review_revision"] == 1


async def test_social_while_rejected_preserves_rejection_without_the_reject_prompt(
    client, provider, session_dir
):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    assert before["review_rejected"] is True
    calls_before = len(provider.calls)
    provider.script_extraction(gratitude_extraction())

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_THANKS, revision=2
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == GRATITUDE_ACKNOWLEDGMENT
    assert payload["reply"] != REJECT_PROMPT
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_THANKS,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True


async def test_social_while_an_issue_is_pending_preserves_the_clarification(
    client, provider, session_dir
):
    session_id = await start_session(client)
    _, invalid = await post_turn(
        client,
        provider,
        session_id,
        text="My order is 123.",
        revision=0,
        extraction=make_extraction(candidates={"order_number": "123"}),
    )
    assert invalid["reply"] == INVALID_ORDER
    assert invalid["pending_clarifications"]["order_number"]["code"] == "invalid"
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(social_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=SOCIAL_GREETING, revision=invalid["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == SOCIAL_ACKNOWLEDGMENT
    assert INVALID_ORDER not in payload["reply"]
    assert payload["pending_clarifications"]["order_number"]["code"] == "invalid"
    assert_session_state(
        payload,
        revision=invalid["revision"] + 1,
        status="collecting",
        slots=invalid["slots"],
        summary=None,
        token=None,
        pending={"order_number": payload["pending_clarifications"]["order_number"]},
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=SOCIAL_GREETING,
        counter_delta=1,
    )


@pytest.mark.parametrize(
    ("intent", "customer_text"),
    [
        ("social", SOCIAL_GREETING),
        ("gratitude", SOCIAL_THANKS),
        ("capability", CAPABILITY_Q),
        ("unrelated", UNRELATED_TEXT),
        ("start_intake", START_TEXT),
    ],
)
async def test_non_confirmation_intents_are_refused_at_the_turn_limit(
    client, provider, session_dir, intent, customer_text
):
    session_id = await seed_session(
        session_dir,
        revision=AT_CAP,
        status="collecting",
        slots=PARTIAL_SLOTS,
        summary=None,
        review_revision=None,
        collection_turn_count=AT_CAP,
    )
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await send_turn(
        client, session_id, text=customer_text, revision=AT_CAP
    )

    await assert_error(
        response, status=409, code="turn_limit", message=TURN_LIMIT_MESSAGE
    )
    # The guard runs before extraction: no model work and no mutation.
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes
