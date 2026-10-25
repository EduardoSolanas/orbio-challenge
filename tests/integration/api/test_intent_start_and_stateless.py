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

This file: starting or resuming intake, and confirm, reject or unrelated turns without state."""

from __future__ import annotations

from gateway.providers.prompts import (
    CAPABILITY,
    MISSING_CATEGORY,
    MISSING_ORDER,
    NO_REVIEW_TO_REJECT,
    NOTHING_TO_CONFIRM,
    REJECT_PROMPT,
)
from tests.integration.support.api import make_extraction, read_session
from tests.integration.support.confirmation import (
    NO_TEXT,
    YES_TEXT,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    rejected_session,
    review_session,
    send_turn,
)
from tests.integration.support.intake import (
    EMPTY_SLOTS,
    FULL_SLOTS,
    FULL_SUMMARY,
    PART_TEXT,
    REVIEW_SUFFIX,
    part_extraction,
    post_turn,
    start_session,
)
from tests.integration.support.intent import (
    PART_SLOTS,
    START_TEXT,
    UNRELATED_TEXT,
    assert_no_policy_suffix,
)


RESUME_TEXT = "Continue my issue"


def start_intake_extraction(has_question: bool = False):
    return make_extraction(intent="start_intake", has_question=has_question)


async def test_explicit_start_intake_asks_the_first_missing_field(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction(has_question=False))

    request_id, response = await send_turn(
        client, session_id, text=START_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == MISSING_ORDER
    assert_no_policy_suffix(payload["reply"])
    assert_session_state(
        payload,
        revision=1,
        status="collecting",
        slots=EMPTY_SLOTS,
        summary=None,
        token=None,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=START_TEXT,
        counter_delta=1,
    )


async def test_explicit_resume_during_intake_reasks_the_pending_missing_field(
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
    provider.script_extraction(start_intake_extraction())

    request_id, response = await send_turn(
        client, session_id, text=RESUME_TEXT, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == MISSING_CATEGORY
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
        text=RESUME_TEXT,
        counter_delta=1,
    )


async def test_explicit_resume_at_review_redisplays_the_current_review(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction())

    request_id, response = await send_turn(
        client, session_id, text=RESUME_TEXT, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RESUME_TEXT,
        counter_delta=1,
    )


async def test_explicit_resume_while_rejected_keeps_the_rejection_prompt(
    client, provider, session_dir
):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(start_intake_extraction())

    request_id, response = await send_turn(
        client, session_id, text=RESUME_TEXT, revision=2
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == REJECT_PROMPT
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
        text=RESUME_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True


async def test_unrelated_statement_uses_the_fixed_capability_reply(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="unrelated", has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=UNRELATED_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == CAPABILITY
    assert MISSING_ORDER not in payload["reply"]
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
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=UNRELATED_TEXT,
        counter_delta=1,
    )


async def test_confirm_without_a_review_explains_and_never_completes(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="confirm"))

    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == NOTHING_TO_CONFIRM
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
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["confirmation"] is None


async def test_reject_without_a_review_does_not_fabricate_rejection(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="reject"))

    request_id, response = await send_turn(
        client, session_id, text=NO_TEXT, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == NO_REVIEW_TO_REJECT
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
        text=NO_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
