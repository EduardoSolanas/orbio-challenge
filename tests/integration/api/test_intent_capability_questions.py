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

This file: capability and policy questions."""

from __future__ import annotations

from gateway.providers.prompts import ABSTENTION, CAPABILITY_REPLY, MISSING_CATEGORY
from gateway.schemas import KnowledgeAnswer
from tests.integration.support.api import make_extraction, read_session
from tests.integration.support.confirmation import (
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    post_confirm,
    receipt_for,
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
    CAPABILITY_Q,
    PART_SLOTS,
    assert_no_policy_suffix,
)


RETURN_Q = "Can I return an unused item?"


PAYMENT_Q = "Which payment methods are accepted?"


RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)


def capability_extraction(has_question: bool = False):
    return make_extraction(intent="capability", has_question=has_question)


def question_extraction():
    return make_extraction(intent="question", has_question=True)


def grounded_return_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
    )


async def test_capability_returns_the_exact_offer_without_retrieval_or_state_change(
    client, provider, session_dir
):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(capability_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=CAPABILITY_Q, revision=0
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == CAPABILITY_REPLY
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
    assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=CAPABILITY_Q,
        counter_delta=1,
    )


async def test_capability_at_a_valid_review_preserves_the_token(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(capability_extraction(has_question=True))

    request_id, response = await send_turn(
        client, session_id, text=CAPABILITY_Q, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == CAPABILITY_REPLY
    assert REVIEW_SUFFIX not in payload["reply"]
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
        text=CAPABILITY_Q,
        counter_delta=1,
    )


async def test_supported_policy_question_during_intake_returns_only_the_cited_answer(
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
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(question_extraction())
    provider.script_answer(grounded_return_answer())

    request_id, response = await send_turn(
        client, session_id, text=RETURN_Q, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=1)
    assert payload["reply"] == RETURN_ANSWER_TEXT
    assert MISSING_CATEGORY not in payload["reply"]
    assert payload["reply"] != part["reply"]
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-returns"]
    assert_session_state(
        payload,
        revision=part["revision"] + 1,
        status="collecting",
        slots=PART_SLOTS,
        summary=None,
        token=None,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RETURN_Q,
        counter_delta=1,
    )
    assert disk["messages"][-1]["citations"] == payload["citations"]
    assert provider.calls[-1][1].question == RETURN_Q


async def test_unsupported_policy_question_during_intake_abstains_without_generation(
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
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(question_extraction())

    request_id, response = await send_turn(
        client, session_id, text=PAYMENT_Q, revision=part["revision"]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == ABSTENTION
    assert MISSING_CATEGORY not in payload["reply"]
    assert payload["citations"] == []
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
        text=PAYMENT_Q,
        counter_delta=1,
    )


async def test_unsupported_policy_question_at_review_preserves_the_token(
    client, provider, session_dir
):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(question_extraction())

    request_id, response = await send_turn(
        client, session_id, text=PAYMENT_Q, revision=1
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert payload["reply"] == ABSTENTION
    assert REVIEW_SUFFIX not in payload["reply"]
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
        text=PAYMENT_Q,
        counter_delta=1,
    )

    # The preserved token still confirms the same review once.
    _, confirmed = await post_confirm(
        client, session_id, expected_revision=2, review_revision=1
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert confirmed_payload["status"] == "completed"
    assert confirmed_payload["reply"] == receipt_for(session_id)
    assert read_session(session_dir, session_id)["confirmation"]["review_revision"] == 1
