"""Message cap and completion behaviour.

Near-cap and completed sessions are prepared through the real repository and a
temporary directory — `seed_session()` validates a `Session` model and saves it
with `JsonSessionRepository.save()` exactly like the app does, so every later
action is served by the real lifespan, routes, engine and JSON store. No Session
JSON is ever hand-written into the request path and no browser turns are clicked
(IC §13.2).

Only the AI service boundary is scripted (`ScriptedProvider`); E/A counts are
the logical `extract` / `answer_knowledge` calls of the action under test.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.support.api import (
    make_extraction,
    persisted_response,
    read_session,
)
from tests.integration.support.confirmation import (
    CHANGE_TEXT,
    YES_TEXT,
    assert_dedicated_confirm_commit,
    assert_error,
    assert_ledger,
    assert_session_state,
    assert_turn_commit,
    change_extraction,
    post_confirm,
    receipt_for,
    send_turn,
)
from tests.integration.support.intake import FULL_SLOTS, FULL_SUMMARY, REVIEW_SUFFIX
from tests.integration.support.limits import (
    AT_CAP,
    PARTIAL_SLOTS,
    TURN_LIMIT_MESSAGE,
    raw_bytes,
    seed_session,
)

SESSION_COMPLETED_MESSAGE = "This support conversation is complete. Start a new conversation if you need help with another issue."
NEAR_CAP = 49


async def get_view(client, session_id: str) -> dict:
    response = await client.get(f"/api/sessions/{session_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def seeded_review(
    client, session_dir: Path, *, revision: int, counter: int
) -> str:
    """A complete, review-eligible session sitting exactly at the cap."""

    session_id = await seed_session(
        session_dir,
        revision=revision,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        review_revision=revision,
        collection_turn_count=counter,
    )
    view = await get_view(client, session_id)
    assert_session_state(
        view,
        revision=revision,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=revision,
    )
    return session_id


async def near_cap_incomplete(client, session_dir: Path) -> str:
    """Counter 49, collecting, urgency still missing: one details turn reaches 50."""

    session_id = await seed_session(
        session_dir,
        revision=NEAR_CAP,
        status="collecting",
        slots=PARTIAL_SLOTS,
        summary=None,
        review_revision=None,
        collection_turn_count=NEAR_CAP,
    )
    view = await get_view(client, session_id)
    assert_session_state(
        view,
        revision=NEAR_CAP,
        status="collecting",
        slots=PARTIAL_SLOTS,
        summary=None,
        token=None,
    )
    return session_id


# validated near cap session49 then complete details50 -------------------------------------------------------------------


async def case_validated_near_cap_session49_then_complete_details50(
    client, provider, session_dir
):
    session_id = await near_cap_incomplete(client, session_dir)
    before = read_session(session_dir, session_id)
    assert before["collection_turn_count"] == NEAR_CAP
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(candidates={"urgency": "high"}))
    text = "High urgency."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=NEAR_CAP
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=AT_CAP,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=AT_CAP,
    )
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["collection_turn_count"] == AT_CAP

    before = disk
    calls_before = len(provider.calls)
    request_id, confirmed = await post_confirm(
        client, session_id, expected_revision=AT_CAP, review_revision=AT_CAP
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    final = assert_dedicated_confirm_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        payload=confirmed_payload,
        request_id=request_id,
        review_revision=AT_CAP,
    )
    assert final["collection_turn_count"] == AT_CAP
    assert final["status"] == "completed"
    assert final["confirmation"]["review_revision"] == AT_CAP


# validated near cap49 then details50 text yes -------------------------------------------------------------------


async def case_validated_near_cap49_then_details50_text_yes(
    client, provider, session_dir
):
    session_id = await near_cap_incomplete(client, session_dir)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(candidates={"urgency": "high"}))
    text = "High urgency."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=NEAR_CAP
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=AT_CAP,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=AT_CAP,
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
    assert disk["collection_turn_count"] == AT_CAP

    before = disk
    calls_before = len(provider.calls)
    provider.script_extraction(make_extraction(intent="confirm"))
    request_id, confirmed = await send_turn(
        client, session_id, text=YES_TEXT, revision=AT_CAP
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        confirmed_payload,
        revision=AT_CAP + 1,
        status="completed",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=None,
    )
    assert confirmed_payload["reply"] == receipt_for(session_id)
    final = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=confirmed_payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=0,
    )
    assert final["collection_turn_count"] == AT_CAP
    assert final["confirmation"]["review_revision"] == AT_CAP
    assert final["status"] == "completed"


# incomplete session counter50 then ordinary details -------------------------------------------------------------------


async def case_incomplete_session_counter50_then_ordinary_details(
    client, provider, session_dir
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
    view = await get_view(client, session_id)
    assert_session_state(
        view,
        revision=AT_CAP,
        status="collecting",
        slots=PARTIAL_SLOTS,
        summary=None,
        token=None,
    )
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)
    _, response = await send_turn(
        client, session_id, text="High urgency.", revision=AT_CAP
    )
    await assert_error(
        response, status=409, code="turn_limit", message=TURN_LIMIT_MESSAGE
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes
    after = read_session(session_dir, session_id)
    assert_session_state(
        after,
        revision=AT_CAP,
        status="collecting",
        slots=PARTIAL_SLOTS,
        summary=None,
        token=None,
    )
    assert after["collection_turn_count"] == AT_CAP
    assert after["requests"] == []


# valid review counter50 then change -------------------------------------------------------------------


async def case_valid_review_counter50_then_change(client, provider, session_dir):
    session_id = await seeded_review(
        client, session_dir, revision=AT_CAP, counter=AT_CAP
    )
    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(change_extraction())
    _, response = await send_turn(client, session_id, text=CHANGE_TEXT, revision=AT_CAP)
    await assert_error(
        response, status=409, code="turn_limit", message=TURN_LIMIT_MESSAGE
    )
    # The possible pure confirmation is classified with one extract, then the
    # non-confirmation is refused without mutation.
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes
    after = read_session(session_dir, session_id)
    assert_session_state(
        after,
        revision=AT_CAP,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=AT_CAP,
    )
    assert after["collection_turn_count"] == AT_CAP
    assert after["requests"] == []


# completed session new edit -------------------------------------------------------------------


async def case_completed_session_new_edit(client, provider, session_dir):
    session_id = await seeded_review(
        client, session_dir, revision=AT_CAP, counter=AT_CAP
    )
    before = read_session(session_dir, session_id)
    _, confirmed = await post_confirm(
        client, session_id, expected_revision=AT_CAP, review_revision=AT_CAP
    )
    assert confirmed.status_code == 200, confirmed.text
    completed = read_session(session_dir, session_id)
    assert completed["status"] == "completed"
    assert completed["revision"] == AT_CAP + 1
    assert completed["collection_turn_count"] == AT_CAP
    assert before["status"] == "awaiting_confirmation"

    before_bytes = raw_bytes(session_dir, session_id)
    calls_before = len(provider.calls)
    _, response = await send_turn(
        client,
        session_id,
        text="Make it high urgency.",
        revision=AT_CAP + 1,
    )
    await assert_error(
        response,
        status=409,
        code="session_completed",
        message=SESSION_COMPLETED_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes
    after = read_session(session_dir, session_id)
    assert after["revision"] == AT_CAP + 1
    assert after["status"] == "completed"
    assert len(after["messages"]) == len(completed["messages"])
    assert len(after["requests"]) == len(completed["requests"])


# completed exact confirmation request replay -------------------------------------------------------------------


async def case_completed_exact_confirmation_request_replay(
    client, provider, session_dir
):
    session_id = await seeded_review(
        client, session_dir, revision=AT_CAP, counter=AT_CAP
    )
    request_id = str(uuid4())
    body = {
        "request_id": request_id,
        "expected_revision": AT_CAP,
        "review_revision": AT_CAP,
    }
    calls_before = len(provider.calls)
    first = await client.post(f"/api/sessions/{session_id}/confirm", json=body)
    assert first.status_code == 200, first.text
    first_payload = first.json()
    completed = read_session(session_dir, session_id)
    assert completed["status"] == "completed"
    assert completed["revision"] == AT_CAP + 1
    assert completed["collection_turn_count"] == AT_CAP
    assert len(completed["messages"]) == 1 + 2 * AT_CAP + 2
    assert len(completed["requests"]) == 1
    before_bytes = raw_bytes(session_dir, session_id)

    replay = await client.post(f"/api/sessions/{session_id}/confirm", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first_payload
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert raw_bytes(session_dir, session_id) == before_bytes
    after = read_session(session_dir, session_id)
    assert after == completed
    assert after["revision"] == AT_CAP + 1
    assert after["collection_turn_count"] == AT_CAP
    assert after["requests"][0]["request_id"] == request_id
    assert after["requests"][0]["response"] == persisted_response(first_payload)


CASES = [
    pytest.param(
        case_validated_near_cap_session49_then_complete_details50,
        id="validated_near_cap_session49_then_complete_details50",
    ),
    pytest.param(
        case_validated_near_cap49_then_details50_text_yes,
        id="validated_near_cap49_then_details50_text_yes",
    ),
    pytest.param(
        case_incomplete_session_counter50_then_ordinary_details,
        id="incomplete_session_counter50_then_ordinary_details",
    ),
    pytest.param(
        case_valid_review_counter50_then_change, id="valid_review_counter50_then_change"
    ),
    pytest.param(case_completed_session_new_edit, id="completed_session_new_edit"),
    pytest.param(
        case_completed_exact_confirmation_request_replay,
        id="completed_exact_confirmation_request_replay",
    ),
]


@pytest.mark.parametrize("case", CASES)
async def test_message_cap_and_completion(case, client, provider, session_dir):
    await case(client, provider, session_dir)
