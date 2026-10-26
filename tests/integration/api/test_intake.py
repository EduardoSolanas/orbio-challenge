"""Intake lifecycle, merge order and corrections.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is
a scripted fake (IC §13.1). Fixture strings are the literal IC §13.1
scripted inputs.
"""

from __future__ import annotations

import pytest

from tests.integration.support.api import make_extraction, read_session
from tests.integration.support.confirmation import change_extraction
from tests.integration.support.intake import (
    CATEGORY_TEXT,
    EMPTY_SLOTS,
    FULL_SLOTS,
    FULL_SUMMARY,
    FULL_TEXT,
    HIGH_TEXT,
    INVALID_ORDER,
    MISSING_CATEGORY,
    MISSING_ORDER,
    PART_TEXT,
    REVIEW_SUFFIX,
    assert_issue,
    assert_normal_turn,
    category_extraction,
    full_extraction,
    high_extraction,
    part_extraction,
    post_turn,
    start_session,
)

CHANGE_TEXT = "Actually make the urgency low."



async def case_full_one_turn(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_clarifications"] == {}
    assert response["revision"] == 1
    assert response["pending_review_revision"] == response["revision"] == 1
    assert response["summary"] == FULL_SUMMARY
    assert response["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert response["citations"] == []
    assert provider.methods() == ["extract"]
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )


async def case_high_then_category_then_part(client, provider, session_dir):
    session_id = await start_session(client)
    calls = 0
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=HIGH_TEXT,
        revision=0,
        extraction=high_extraction(),
    )
    assert response["slots"] == {**EMPTY_SLOTS, "urgency": "high"}
    assert response["reply"] == MISSING_ORDER
    assert response["pending_clarifications"] == {}
    assert response["status"] == "collecting"
    assert response["pending_review_revision"] is None
    assert response["summary"] is None
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=calls,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=HIGH_TEXT,
    )
    calls += 1

    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=CATEGORY_TEXT,
        revision=1,
        extraction=category_extraction(),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "category": "damaged_item",
        "urgency": "high",
    }
    assert response["reply"] == MISSING_ORDER
    assert response["status"] == "collecting"
    assert response["pending_review_revision"] is None
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=calls,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=CATEGORY_TEXT,
    )
    calls += 1

    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=2,
        extraction=part_extraction(),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY
    assert response["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=calls,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=PART_TEXT,
    )
    assert provider.methods() == ["extract", "extract", "extract"]


async def case_repeat_part_equal_facts(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "order_number": "NS-000123",
        "description": "My order arrived broken",
    }
    assert response["reply"] == MISSING_CATEGORY
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=PART_TEXT,
    )

    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=1,
        extraction=part_extraction(),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "order_number": "NS-000123",
        "description": "My order arrived broken",
    }
    assert response["pending_clarifications"] == {}
    assert response["reply"] == MISSING_CATEGORY
    assert response["revision"] == 2
    disk = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=PART_TEXT,
    )
    assert len(disk["messages"]) == 5
    assert len(disk["requests"]) == 2
    assert provider.methods() == ["extract", "extract"]


async def case_invalid_order_plus_valid_description_urgency(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    text = "My order NS-12345 arrived broken. It is high urgency; can I return it?"
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            has_question=True,
            candidates={
                "order_number": "NS-12345",
                "description": "  My order arrived broken  ",
                "urgency": "high",
            },
        ),
    )
    assert response["slots"] == {
        "order_number": None,
        "category": None,
        "description": "My order arrived broken",
        "urgency": "high",
    }
    assert response["pending_clarifications"] == {
        "order_number": {
            "code": "invalid",
            "candidate": "NS-12345",
            "question": INVALID_ORDER,
        }
    }
    assert response["reply"] == (
        f"{INVALID_ORDER} Please ask your policy question in a separate message."
    )
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_review_then_change(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["pending_review_revision"] == 1
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    low_summary = FULL_SUMMARY.replace("Urgency: high", "Urgency: low")
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=CHANGE_TEXT,
        revision=1,
        extraction=change_extraction(),
    )
    assert response["slots"] == {**FULL_SLOTS, "urgency": "low"}
    assert response["status"] == "awaiting_confirmation"
    assert response["status"] != "completed"
    assert response["pending_clarifications"] == {}
    assert response["pending_review_revision"] == 2
    assert response["summary"] == low_summary
    assert response["reply"] == low_summary + REVIEW_SUFFIX
    disk = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=CHANGE_TEXT,
    )
    assert disk["status"] == "awaiting_confirmation"
    assert disk["summary"] == low_summary


async def case_review_then_non_explicit_different_order(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    text = "My energy account reference is NS-654321."
    conflict_question = (
        "You previously gave NS-000123. "
        "Should I keep that or use NS-654321 for energy account reference?"
    )
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(candidates={"order_number": "NS-654321"}),
    )
    assert response["slots"] == FULL_SLOTS
    assert_issue(response, "order_number", "conflict", "NS-654321", conflict_question)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert response["revision"] == 2
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_conflict_then_explicit_choose_new_order(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My energy account reference is NS-654321.",
        revision=1,
        extraction=make_extraction(candidates={"order_number": "NS-654321"}),
    )
    assert response["pending_clarifications"]["order_number"]["code"] == "conflict"
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My energy account reference is NS-654321.",
    )

    text = "Use NS-654321."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=2,
        extraction=make_extraction(
            intent="correction",
            candidates={"order_number": "NS-654321"},
            explicit_corrections=["order_number"],
        ),
    )
    assert response["slots"] == {**FULL_SLOTS, "order_number": "NS-654321"}
    assert response["pending_clarifications"] == {}
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY.replace(
        "Account reference: NS-000123", "Account reference: NS-654321"
    )
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=2,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )
    assert provider.methods() == ["extract", "extract", "extract"]


async def case_conflict_then_explicit_retain_old_order(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My energy account reference is NS-654321.",
        revision=1,
        extraction=make_extraction(candidates={"order_number": "NS-654321"}),
    )
    assert response["pending_clarifications"]["order_number"]["code"] == "conflict"
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My energy account reference is NS-654321.",
    )

    text = "Keep NS-000123."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=2,
        extraction=make_extraction(
            intent="correction",
            candidates={"order_number": "NS-000123"},
            explicit_corrections=["order_number"],
        ),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["pending_clarifications"] == {}
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=2,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_review_then_ambiguous_fields_urgency_null_candidate(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["pending_review_revision"] == 1
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    text = "I am not sure about the urgency."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(
            candidates={}, ambiguous_fields=["urgency"]
        ),
    )
    assert response["slots"] == FULL_SLOTS
    assert_issue(
        response, "urgency", "ambiguous", None, "Please clarify your urgency."
    )
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_existing_pending_issue_followed_by_all_null_extraction(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My energy account reference is NS-12345.",
        revision=0,
        extraction=make_extraction(candidates={"order_number": "NS-12345"}),
    )
    assert_issue(response, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My energy account reference is NS-12345.",
    )

    text = "Thanks, that is all for now."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(candidates={}),
    )
    assert response["slots"] == EMPTY_SLOTS
    assert_issue(response, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    disk = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )
    assert disk["pending_clarifications"] == {
        "order_number": {
            "code": "invalid",
            "candidate": "NS-12345",
            "question": INVALID_ORDER,
        }
    }


CASES = [
    pytest.param(case_full_one_turn, id="full_one_turn"),
    pytest.param(case_high_then_category_then_part, id="high_then_category_then_part"),
    pytest.param(case_repeat_part_equal_facts, id="repeat_part_equal_facts"),
    pytest.param(case_invalid_order_plus_valid_description_urgency, id="invalid_order_plus_valid_description_urgency"),
    pytest.param(case_review_then_change, id="review_then_change"),
    pytest.param(case_review_then_non_explicit_different_order, id="review_then_non_explicit_different_order"),
    pytest.param(case_conflict_then_explicit_choose_new_order, id="conflict_then_explicit_choose_new_order"),
    pytest.param(case_conflict_then_explicit_retain_old_order, id="conflict_then_explicit_retain_old_order"),
    pytest.param(case_review_then_ambiguous_fields_urgency_null_candidate, id="review_then_ambiguous_fields_urgency_null_candidate"),
    pytest.param(case_existing_pending_issue_followed_by_all_null_extraction, id="existing_pending_issue_followed_by_all_null_extraction"),
]


@pytest.mark.parametrize("case", CASES)
async def test_intake_lifecycle(case, client, provider, session_dir):
    await case(client, provider, session_dir)
