"""Integration tests for order verification over the real Backend API.

The gateway app, lifespan, routes, engine and JSON repository run against an
isolated temp store; only the external AI provider is scripted. The shipped
Backend API is served by real Uvicorn on a loopback port, so every verification
and lookup crosses a genuine TCP/HTTP boundary through `BackendApiClient`: test verification with intake."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from gateway.localization import build_review_reply, build_summary, text
from backend.api import DEMO_VERIFICATION_CODE, FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE
from gateway.schemas import KnowledgeAnswer, Slots
from tests.integration.order_verification.support import (
    ORDER_NUMBER_PROMPT,
    ORDER_NUMBER_TEXT,
    ORDER_RESULT,
    ORDER_STATUS_Q,
    OTHER_ORDER,
    RETURN_ANSWER_TEXT,
    RETURN_Q,
    VERIFICATION_PROMPT,
    create_session,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support.api import make_extraction, read_session


PART = "My order NS-000123 arrived broken."

CATEGORY_QUESTION = text("intake.category", "en")


async def test_generic_policy_faq_does_not_trigger_verification(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(
        make_extraction(intent="question", has_question=True)
    )
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )

    response = await send_turn(client, session_id, RETURN_Q, 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == RETURN_ANSWER_TEXT
    assert body["order_verification_required"] is False
    assert VERIFICATION_PROMPT not in response.text
    assert verification.pending_order(UUID(session_id)) is None
    assert verification.grant(UUID(session_id)) is None
    assert provider.methods() == ["extract", "answer_knowledge"]


async def test_recording_customer_supplied_details_does_not_require_verification(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(
        make_extraction(
            candidates={
                "order_number": FIXTURE_ORDER_NUMBER,
                "description": "My order arrived broken",
            }
        )
    )

    response = await send_turn(client, session_id, PART, 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == CATEGORY_QUESTION
    assert body["order_verification_required"] is False
    assert body["slots"]["order_number"] == FIXTURE_ORDER_NUMBER
    assert VERIFICATION_PROMPT not in response.text
    assert verification.pending_order(UUID(session_id)) is None
    assert verification.grant(UUID(session_id)) is None


async def test_verification_during_an_issue_intake_resumes_without_reasking_the_order(
    client, provider, verification, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(
        make_extraction(
            candidates={
                "order_number": FIXTURE_ORDER_NUMBER,
                "description": "My order arrived broken",
            }
        )
    )
    collected = await send_turn(client, session_id, PART, 0)
    assert collected.json()["reply"] == CATEGORY_QUESTION
    assert collected.json()["order_verification_required"] is False
    assert verification.grant(UUID(session_id)) is None

    provider.script_extraction(order_lookup())
    asked = await send_turn(client, session_id, ORDER_STATUS_Q, 1)
    assert asked.json()["reply"] == VERIFICATION_PROMPT
    assert asked.json()["order_verification_required"] is True
    assert verification.pending_order(UUID(session_id)) == FIXTURE_ORDER_NUMBER

    assert (await verify(client, session_id)).json() == {
        "verified": True,
        "error": None,
    }
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER

    provider.script_extraction(
        make_extraction(candidates={"category": "damaged_item"})
    )
    second = await send_turn(client, session_id, "It is a damaged item.", 2)
    body = second.json()
    assert body["reply"] == text("intake.urgency", "en")
    assert body["order_verification_required"] is False
    assert body["slots"]["order_number"] == FIXTURE_ORDER_NUMBER
    assert body["slots"]["category"] == "damaged_item"
    assert ORDER_NUMBER_PROMPT not in body["reply"]
    assert VERIFICATION_PROMPT not in body["reply"]
    assert provider.calls[-1][1].order_verified is True
    assert provider.calls[-1][1].verified_order_number == FIXTURE_ORDER_NUMBER

    provider.script_extraction(make_extraction(candidates={"urgency": "high"}))
    third = await send_turn(client, session_id, "High urgency.", 3)
    body = third.json()
    expected_slots = Slots(
        order_number=FIXTURE_ORDER_NUMBER,
        category="damaged_item",
        description="My order arrived broken",
        urgency="high",
    )
    assert body["status"] == "awaiting_confirmation"
    assert body["reply"] == build_review_reply(expected_slots, "en")
    assert body["summary"] == build_summary(expected_slots, "en")
    assert body["pending_review_revision"] == 4

    confirmed = await client.post(
        f"/api/sessions/{session_id}/confirm",
        json={
            "request_id": str(uuid4()),
            "expected_revision": 4,
            "review_revision": 4,
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "completed"
    saved = read_session(session_dir, session_id)
    assert saved["slots"]["order_number"] == FIXTURE_ORDER_NUMBER
    serialized = json.dumps(saved)
    assert FIXTURE_POSTCODE not in serialized
    assert DEMO_VERIFICATION_CODE not in serialized
    assert '"123456"' not in serialized


async def test_a_new_session_never_inherits_a_verification_grant(
    client, provider, verification, backend_api
):
    first = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, first, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, first, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, first)).json()["verified"] is True
    assert verification.grant(UUID(first)) == FIXTURE_ORDER_NUMBER

    fresh = await create_session(client)
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    response = await send_turn(client, fresh, ORDER_NUMBER_TEXT, 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert verification.grant(UUID(fresh)) is None
    assert backend_api.order_gets() == []


async def test_accepting_the_same_verified_order_into_intake_keeps_the_grant(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True

    provider.script_extraction(
        make_extraction(
            candidates={
                "order_number": FIXTURE_ORDER_NUMBER,
                "description": "My order arrived broken",
            }
        )
    )
    reported = await send_turn(client, session_id, PART, 2)

    assert reported.status_code == 200
    assert reported.json()["slots"]["order_number"] == FIXTURE_ORDER_NUMBER
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER

    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    lookup = await send_turn(client, session_id, ORDER_STATUS_Q, 3)

    assert lookup.json()["reply"] == ORDER_RESULT
    assert lookup.json()["order_verification_required"] is False


async def test_accepting_a_different_order_clears_the_verified_grant(
    client, provider, verification, backend_api
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER

    provider.script_extraction(
        make_extraction(candidates={"order_number": OTHER_ORDER})
    )
    changed = await send_turn(client, session_id, "Use order NS-000999.", 2)

    assert changed.status_code == 200
    assert changed.json()["slots"]["order_number"] == OTHER_ORDER
    assert verification.grant(UUID(session_id)) is None

    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    denied = await send_turn(client, session_id, ORDER_STATUS_Q, 3)

    assert denied.status_code == 200
    assert denied.json()["reply"] == VERIFICATION_PROMPT
    assert denied.json()["order_verification_required"] is True
    assert backend_api.order_gets() == []


async def test_policy_faq_with_an_unverified_order_in_slots_skips_verification(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(
        make_extraction(
            candidates={
                "order_number": FIXTURE_ORDER_NUMBER,
                "description": "My order arrived broken",
            }
        )
    )
    await send_turn(client, session_id, PART, 0)

    provider.script_extraction(make_extraction(intent="question", has_question=True))
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )
    response = await send_turn(client, session_id, RETURN_Q, 1)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == RETURN_ANSWER_TEXT
    assert body["order_verification_required"] is False
    assert VERIFICATION_PROMPT not in body["reply"]
    assert provider.methods() == [
        "extract",
        "extract",
        "answer_knowledge",
    ]
