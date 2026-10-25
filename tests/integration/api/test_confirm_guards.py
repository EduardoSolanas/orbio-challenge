"""Dedicated confirm guards: stale review token, incomplete intake and a blocking
ambiguity.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is a
scripted fake (IC §13.1). Each action is checked on the wire (flag, token,
summary, status, revision, messages) and in the committed JSON, with the provider
ledger counted before and after (E = extract, A = answer_knowledge)."""

from __future__ import annotations

import pytest

from tests.integration.support.api import read_session
from tests.integration.support.confirmation import (
    AMBIGUOUS_URGENCY,
    REVIEW_NOT_READY_MESSAGE,
    STALE_REVIEW_MESSAGE,
    ambiguous_urgency_extraction,
    assert_error,
    assert_ledger,
    assert_session_state,
    post_confirm,
    review_session,
)
from tests.integration.support.intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    assert_issue,
    part_extraction,
    start_session,
    turn_body,
)


async def case_dedicated_confirm_stale_review_token(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    _, response = await post_confirm(
        client, session_id, expected_revision=1, review_revision=0
    )
    await assert_error(
        response,
        status=409,
        code="stale_review",
        message=STALE_REVIEW_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    after = read_session(session_dir, session_id)
    assert after == before
    assert after["status"] == "awaiting_confirmation"
    assert after["pending_review_revision"] == 1
    assert after["summary"] == FULL_SUMMARY
    assert after["confirmation"] is None
    assert len(after["messages"]) == 3
    assert len(after["requests"]) == 1


async def case_dedicated_confirm_on_incomplete_collecting_intake(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    provider.script_extraction(part_extraction())
    turn = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body("My order NS-000123 arrived broken.", 0),
    )
    assert turn.status_code == 200, turn.text
    incomplete = read_session(session_dir, session_id)
    assert_session_state(
        incomplete,
        revision=1,
        status="collecting",
        slots={**FULL_SLOTS, "category": None, "urgency": None},
        summary=None,
        token=None,
    )
    assert incomplete["pending_clarifications"] == {}
    calls_before = len(provider.calls)
    _, response = await post_confirm(
        client, session_id, expected_revision=1, review_revision=0
    )
    await assert_error(
        response,
        status=409,
        code="review_not_ready",
        message=REVIEW_NOT_READY_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    after = read_session(session_dir, session_id)
    assert after == incomplete
    assert after["pending_review_revision"] is None
    assert after["summary"] is None
    assert after["status"] == "collecting"
    assert after["revision"] == 1
    assert before["revision"] == 0


async def case_dedicated_confirm_while_explicit_ambiguity_issue_blocks(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    provider.script_extraction(ambiguous_urgency_extraction())
    text = "I am not sure about the urgency."
    turn = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text, 1)
    )
    assert turn.status_code == 200, turn.text
    blocked = turn.json()
    assert_issue(blocked, "urgency", "ambiguous", None, AMBIGUOUS_URGENCY)
    assert_session_state(
        blocked,
        revision=2,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
        pending={
            "urgency": {
                "code": "ambiguous",
                "candidate": None,
                "question": AMBIGUOUS_URGENCY,
            }
        },
    )
    disk_blocked = read_session(session_dir, session_id)
    assert disk_blocked["pending_review_revision"] is None
    assert disk_blocked["slots"] == FULL_SLOTS
    assert disk_blocked["summary"] is None

    calls_before = len(provider.calls)
    _, response = await post_confirm(
        client, session_id, expected_revision=2, review_revision=1
    )
    await assert_error(
        response,
        status=409,
        code="review_not_ready",
        message=REVIEW_NOT_READY_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    after = read_session(session_dir, session_id)
    assert after == disk_blocked
    assert after["pending_review_revision"] is None
    assert after["slots"] == FULL_SLOTS
    assert after["confirmation"] is None
    assert before["revision"] == 1


CASES = [
    pytest.param(
        case_dedicated_confirm_stale_review_token,
        id="dedicated_confirm_stale_review_token",
    ),
    pytest.param(
        case_dedicated_confirm_on_incomplete_collecting_intake,
        id="dedicated_confirm_on_incomplete_collecting_intake",
    ),
    pytest.param(
        case_dedicated_confirm_while_explicit_ambiguity_issue_blocks,
        id="dedicated_confirm_while_explicit_ambiguity_issue_blocks",
    ),
]


@pytest.mark.parametrize("case", CASES)
async def test_dedicated_confirm_guard_cases(case, client, provider, session_dir):
    await case(client, provider, session_dir)
