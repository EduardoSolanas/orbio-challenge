"""Confirming or rejecting a valid review: dedicated confirm, text yes, a changed
urgency, a policy question and no.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is a
scripted fake (IC §13.1). Each action is checked on the wire (flag, token,
summary, status, revision, messages) and in the committed JSON, with the provider
ledger counted before and after (E = extract, A = answer_knowledge)."""

from __future__ import annotations

import pytest

from gateway.schemas import KnowledgeAnswer
from tests.integration.support.api import read_session
from tests.integration.support.confirmation import (
    LOW_SUMMARY,
    NO_TEXT,
    RETURN_ANSWER_TEXT,
    RETURN_Q_TEXT,
    YES_TEXT,
    assert_dedicated_confirm_commit,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    post_confirm,
    receipt_for,
    reject_extraction,
    review_session,
    send_turn,
    yes_changed_urgency_extraction,
    yes_equal_extraction,
    yes_extraction,
    yes_policy_question_extraction,
)
from tests.integration.support.intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    REJECT_PROMPT,
    REVIEW_SUFFIX,
)


async def case_valid_review_dedicated_confirm(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    assert_session_state(
        before,
        revision=1,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert before["review_rejected"] is False
    assert before["collection_turn_count"] == 1

    calls_before = len(provider.calls)
    request_id, response = await post_confirm(
        client, session_id, expected_revision=1, review_revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert_dedicated_confirm_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        payload=payload,
        request_id=request_id,
        review_revision=1,
    )


async def case_valid_review_text_yes(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(
        provider,
        calls_before=calls_before,
        extracts=1,
        answers=0,
        context={
            "current_message": YES_TEXT,
            "slots": before["slots"],
            "pending_clarifications": {},
            "status": "awaiting_confirmation",
        },
    )
    receipt = receipt_for(session_id)
    assert_session_state(
        payload,
        revision=2,
        status="completed",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=None,
    )
    assert payload["reply"] == receipt
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=0,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"]["review_revision"] == 1
    assert disk["confirmation"]["confirmed_at"].endswith("Z")


async def case_valid_review_confirm_candidates_equal_accepted_slots(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_equal_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=2,
        status="completed",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=None,
    )
    assert payload["reply"] == receipt_for(session_id)
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=0,
    )
    assert disk["confirmation"]["review_revision"] == 1
    assert disk["review_rejected"] is False


async def case_review_yes_plus_changed_urgency(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_changed_urgency_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(
        provider,
        calls_before=calls_before,
        extracts=1,
        answers=0,
        context={
            "current_message": YES_TEXT,
            "slots": FULL_SLOTS,
            "pending_clarifications": {},
            "status": "awaiting_confirmation",
        },
    )
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=low_slots,
        summary=LOW_SUMMARY,
        token=2,
    )
    assert payload["reply"] == LOW_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["status"] == "awaiting_confirmation"
    assert disk["confirmation"] is None
    assert disk["review_rejected"] is False


async def case_review_yes_plus_policy_question(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_policy_question_extraction())
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )
    request_id, response = await send_turn(
        client, session_id, text=RETURN_Q_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=1)
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert payload["reply"] == RETURN_ANSWER_TEXT
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-returns"]
    assert payload["citations"][0]["title"]
    assert payload["citations"][0]["excerpt"]
    assert payload["citations"][0]["kb_version"]
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RETURN_Q_TEXT,
        counter_delta=1,
    )
    assert disk["pending_review_revision"] == 1
    assert disk["summary"] == FULL_SUMMARY
    assert disk["confirmation"] is None


async def case_review_no(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(reject_extraction())
    request_id, response = await send_turn(
        client, session_id, text=NO_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=2,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == REJECT_PROMPT
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=NO_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["confirmation"] is None


CASES = [
    pytest.param(
        case_valid_review_dedicated_confirm,
        id="valid_review_dedicated_confirm",
    ),
    pytest.param(case_valid_review_text_yes, id="valid_review_text_yes"),
    pytest.param(
        case_valid_review_confirm_candidates_equal_accepted_slots,
        id="valid_review_confirm_candidates_equal_accepted_slots",
    ),
    pytest.param(
        case_review_yes_plus_changed_urgency,
        id="review_yes_plus_changed_urgency",
    ),
    pytest.param(
        case_review_yes_plus_policy_question,
        id="review_yes_plus_policy_question",
    ),
    pytest.param(case_review_no, id="review_no"),
]


@pytest.mark.parametrize("case", CASES)
async def test_review_confirmation_cases(case, client, provider, session_dir):
    await case(client, provider, session_dir)
