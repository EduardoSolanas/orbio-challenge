"""Recovering from a rejected review: corrections, reaffirmations, invalid input,
unrelated turns and questions.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is a
scripted fake (IC §13.1). Each action is checked on the wire (flag, token,
summary, status, revision, messages) and in the committed JSON, with the provider
ledger counted before and after (E = extract, A = answer_knowledge)."""

from __future__ import annotations

import pytest

from app.prompts import CAPABILITY
from app.schemas import KnowledgeAnswer
from tests.integration.support.api import read_session
from tests.integration.support.confirmation import (
    CHANGE_TEXT,
    LOW_SUMMARY,
    POLICY_QUESTION_SUFFIX,
    RETURN_ANSWER_TEXT,
    RETURN_Q_TEXT,
    YES_TEXT,
    assert_dedicated_confirm_commit,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    change_extraction,
    correction_with_question_extraction,
    equal_explicit_correction_extraction,
    invalid_correction_extraction,
    post_confirm,
    reject_with_correction_extraction,
    rejected_session,
    review_session,
    send_turn,
    unrelated_extraction,
    valid_correction_plus_invalid_sibling_extraction,
    yes_equal_extraction,
    yes_extraction,
    yes_policy_question_extraction,
)
from tests.integration.support.intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    INVALID_ORDER,
    REJECT_PROMPT,
    REVIEW_SUFFIX,
    assert_issue,
)


async def case_rejected_then_valid_changed_correction(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    assert before["review_rejected"] is True
    calls_before = len(provider.calls)
    provider.script_extraction(change_extraction())
    request_id, response = await send_turn(
        client, session_id, text=CHANGE_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=low_slots,
        summary=LOW_SUMMARY,
        token=3,
    )
    assert payload["reply"] == LOW_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=CHANGE_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"] is None

    # Only a later confirmation completes the recovered intake.
    recovered = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    request_id, confirmed = await post_confirm(
        client, session_id, expected_revision=3, review_revision=3
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert_dedicated_confirm_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=recovered,
        payload=confirmed_payload,
        request_id=request_id,
        review_revision=3,
    )


async def case_rejected_then_explicit_equal_accepted_correction(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(equal_explicit_correction_extraction())
    text = "Keep the high urgency."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=3,
    )
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text="Keep the high urgency.",
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"] is None


async def case_rejected_then_unambiguous_all_null_yes(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=3,
    )
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"] is None


async def case_rejected_then_unambiguous_equal_value_reaffirmation(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_equal_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=3,
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
    assert disk["review_rejected"] is False
    assert disk["slots"] == FULL_SLOTS


async def case_rejected_then_invalid_correction(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(invalid_correction_extraction())
    text = "Change the order to NS-12345."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_issue(payload, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
        pending={
            "order_number": {
                "code": "invalid",
                "candidate": "NS-12345",
                "question": INVALID_ORDER,
            }
        },
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["confirmation"] is None


async def case_rejected_then_valid_urgency_correction_plus_invalid_order(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(valid_correction_plus_invalid_sibling_extraction())
    text = "Make it low urgency, and the order is NS-12345."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_issue(payload, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=low_slots,
        summary=None,
        token=None,
        pending={
            "order_number": {
                "code": "invalid",
                "candidate": "NS-12345",
                "question": INVALID_ORDER,
            }
        },
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["pending_review_revision"] is None


async def case_rejected_then_unrelated_statement(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(unrelated_extraction())
    text = "It is raining today."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == CAPABILITY
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["confirmation"] is None


async def case_rejected_then_return_q(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_policy_question_extraction())
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )
    request_id, response = await send_turn(
        client, session_id, text=RETURN_Q_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=1)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == RETURN_ANSWER_TEXT
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-returns"]
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RETURN_Q_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["summary"] is None
    assert disk["pending_review_revision"] is None


async def case_reject_accepted_correction_same_extraction(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(reject_with_correction_extraction())
    text = "No, make it low urgency."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=2,
        status="collecting",
        slots=low_slots,
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
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["slots"] == low_slots
    assert disk["confirmation"] is None


async def case_rejected_then_correction_has_question_true(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(correction_with_question_extraction())
    text = "Make it low urgency. Can I return an unused item?"
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=low_slots,
        summary=None,
        token=None,
    )
    assert payload["reply"] == f"{REJECT_PROMPT} {POLICY_QUESTION_SUFFIX}"
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["slots"] == low_slots


CASES = [
    pytest.param(
        case_rejected_then_valid_changed_correction,
        id="rejected_then_valid_changed_correction",
    ),
    pytest.param(
        case_rejected_then_explicit_equal_accepted_correction,
        id="rejected_then_explicit_equal_accepted_correction",
    ),
    pytest.param(
        case_rejected_then_unambiguous_all_null_yes,
        id="rejected_then_unambiguous_all_null_yes",
    ),
    pytest.param(
        case_rejected_then_unambiguous_equal_value_reaffirmation,
        id="rejected_then_unambiguous_equal_value_reaffirmation",
    ),
    pytest.param(
        case_rejected_then_invalid_correction,
        id="rejected_then_invalid_correction",
    ),
    pytest.param(
        case_rejected_then_valid_urgency_correction_plus_invalid_order,
        id="rejected_then_valid_urgency_correction_plus_invalid_order",
    ),
    pytest.param(
        case_rejected_then_unrelated_statement,
        id="rejected_then_unrelated_statement",
    ),
    pytest.param(case_rejected_then_return_q, id="rejected_then_return_q"),
    pytest.param(
        case_reject_accepted_correction_same_extraction,
        id="reject_accepted_correction_same_extraction",
    ),
    pytest.param(
        case_rejected_then_correction_has_question_true,
        id="rejected_then_correction_has_question_true",
    ),
]


@pytest.mark.parametrize("case", CASES)
async def test_rejection_recovery_cases(case, client, provider, session_dir):
    await case(client, provider, session_dir)
