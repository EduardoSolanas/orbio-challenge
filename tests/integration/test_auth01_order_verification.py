"""Integration tests for AUTH-01 order verification over the real Order API.

The gateway app, lifespan, routes, engine and JSON repository run against an
isolated temp store; only the external AI provider is scripted. The shipped
Order API is served by real Uvicorn on a loopback port, so every verification
and lookup crosses a genuine TCP/HTTP boundary through `OrderApiClient`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.errors import INVALID_ORDER_API_RESPONSE, ORDER_API_TIMEOUT
from app.localization import build_review_reply, build_summary, render, text
from app.main import create_app
from app.schemas import KnowledgeAnswer, Slots, ToolRequest
from app.order_api import DEMO_VERIFICATION_CODE, FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE
from app.order_client import OrderApiClient
from app.verification import (
    MAX_VERIFICATION_ATTEMPTS,
    VERIFICATION_TTL_SECONDS,
    VerificationRegistry,
)
from tests.integration.support_order_api import LocalOrderApiServer
from tests.integration.support_api import (
    ScriptedProvider,
    build_settings,
    make_extraction,
    persisted_response,
    read_session,
    running_client,
)

OTHER_ORDER = "NS-000999"
WRONG_CODE = "123456"
ORDER_STATUS_Q = "Where is my order?"
ORDER_NUMBER_TEXT = "The energy account reference is NS-000123."
RETURN_Q = "Can I return an unused item?"
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused "
    "item in its original packaging. Refund eligibility requires support review."
)
PART = "My order NS-000123 arrived broken."
CATEGORY_QUESTION = text("intake.category", "en")
VERIFICATION_PROMPT = text("auth.verification_prompt", "en")
ORDER_NUMBER_PROMPT = text("auth.order_number_prompt", "en")
ORDER_RESULT = render(
    "auth.order_result",
    "en",
    {
        "order_number": FIXTURE_ORDER_NUMBER,
        "first_name": "Casey",
        "item_names": "Electricity and gas supply",
        "status": text("auth.status_active", "en"),
    },
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def order_lookup(
    *,
    order_number: str | None = None,
    tool: bool = False,
    language: str = "en",
    frustration: str = "none",
):
    return make_extraction(
        intent="order_lookup",
        has_question=True,
        candidates={"order_number": order_number},
        detected_language=language,
        frustration=frustration,
        tool_request=(
            ToolRequest(name="lookup_verified_order", arguments={}) if tool else None
        ),
    )


def full_intake():
    return make_extraction(
        candidates={
            "order_number": FIXTURE_ORDER_NUMBER,
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": "high",
        }
    )


async def create_session(client) -> str:
    response = await client.post("/api/sessions", json={})
    assert response.status_code == 201
    return response.json()["session_id"]


async def send_turn(client, session_id: str, message: str, revision: int):
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json={
            "request_id": str(uuid4()),
            "expected_revision": revision,
            "text": message,
            "input_mode": "text",
        },
    )
    return response


async def verify(
    client,
    session_id: str,
    postcode: str = FIXTURE_POSTCODE,
    code: str = DEMO_VERIFICATION_CODE,
):
    return await client.post(
        f"/api/sessions/{session_id}/order-verification",
        json={"delivery_postcode": postcode, "verification_code": code},
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def verification(clock: FakeClock) -> VerificationRegistry:
    return VerificationRegistry(clock=clock)


@pytest.fixture
def order_api() -> LocalOrderApiServer:
    with LocalOrderApiServer() as server:
        yield server


@pytest.fixture
def app(settings, provider, verification, order_api):
    return create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        order_api_client=OrderApiClient(order_api.base_url),
    )


@pytest.fixture
async def client(app):
    async with running_client(app) as active_client:
        yield active_client


async def test_order_status_without_order_number_asks_for_it_without_auth_or_rag(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())

    response = await send_turn(client, session_id, ORDER_STATUS_Q, 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == ORDER_NUMBER_PROMPT
    assert body["order_verification_required"] is False
    assert body["slots"] == {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert provider.methods() == ["extract"]
    assert verification.pending_order(UUID(session_id)) is None
    assert verification.grant(UUID(session_id)) is None


async def test_valid_order_prompts_verification_and_wrong_code_changes_nothing(
    client, provider, verification, order_api, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    assert (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).status_code == 200
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))

    prompt = await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)

    assert prompt.status_code == 200
    body = prompt.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert verification.pending_order(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert verification.grant(UUID(session_id)) is None
    assert body["slots"]["order_number"] is None
    saved_bytes = (session_dir / f"{session_id}.json").read_bytes()

    wrong = await verify(client, session_id, code=WRONG_CODE)

    assert wrong.status_code == 200
    assert wrong.json() == {"verified": False, "error": "verification_failed"}
    assert (session_dir / f"{session_id}.json").read_bytes() == saved_bytes
    assert verification.grant(UUID(session_id)) is None
    assert order_api.order_gets() == []
    assert FIXTURE_POSTCODE not in wrong.text
    assert WRONG_CODE not in wrong.text
    assert "Casey" not in wrong.text


async def test_wrong_postcode_and_unknown_order_share_the_same_public_error(
    client, provider, verification, order_api
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)

    wrong_postcode = await verify(client, session_id, postcode="SW1A 1AB")
    assert wrong_postcode.status_code == 200
    assert wrong_postcode.json() == {
        "verified": False,
        "error": "verification_failed",
    }

    unknown_order = await create_session(client)
    provider.script_extraction(order_lookup(order_number=OTHER_ORDER))
    await send_turn(client, unknown_order, ORDER_NUMBER_TEXT, 0)
    unknown = await verify(client, unknown_order)
    assert unknown.status_code == 200
    assert unknown.json() == {
        "verified": False,
        "error": "verification_failed",
    }
    assert order_api.order_gets() == []


async def test_normalized_postcode_and_demo_code_grant_one_allowlisted_lookup(
    client, provider, verification, order_api, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    before = (session_dir / f"{session_id}.json").read_bytes()

    success = await verify(client, session_id, postcode=" sw1a 1aa ")

    assert success.status_code == 200
    assert success.json() == {"verified": True, "error": None}
    assert (session_dir / f"{session_id}.json").read_bytes() == before
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER

    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    lookup = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert lookup.status_code == 200
    body = lookup.json()
    assert body["reply"] == ORDER_RESULT
    assert body["order_verification_required"] is False
    assert order_api.order_gets() == [FIXTURE_ORDER_NUMBER]
    assert provider.methods() == ["extract", "extract", "extract"]
    assert "answer_knowledge" not in provider.methods()
    assert FIXTURE_POSTCODE not in lookup.text
    assert DEMO_VERIFICATION_CODE not in lookup.text

    context = provider.calls[-1][1]
    assert context.verified_order_number == FIXTURE_ORDER_NUMBER
    assert context.order_verified is True
    serialized = json.dumps(context.model_dump(mode="json"))
    assert FIXTURE_POSTCODE not in serialized
    assert DEMO_VERIFICATION_CODE not in serialized
    assert '"123456"' not in serialized


async def test_tool_request_before_verification_is_refused(
    client, provider, order_api
):
    session_id = await create_session(client)
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )

    response = await send_turn(client, session_id, ORDER_STATUS_Q, 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert order_api.order_gets() == []
    assert "Casey" not in response.text
    assert "Electricity and gas supply" not in response.text


async def test_tool_request_for_another_order_is_rejected(
    client, provider, verification, order_api
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER

    provider.script_extraction(order_lookup(order_number=OTHER_ORDER, tool=True))
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert order_api.order_gets() == []
    assert verification.grant(UUID(session_id)) is None
    assert verification.pending_order(UUID(session_id)) == OTHER_ORDER
    assert "Casey" not in response.text


async def test_expired_grant_denies_lookup_until_reverified(
    client, provider, verification, order_api, clock
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True

    clock.advance(VERIFICATION_TTL_SECONDS + 1)
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert response.status_code == 200
    assert response.json()["order_verification_required"] is True
    assert response.json()["reply"] == VERIFICATION_PROMPT
    assert order_api.order_gets() == []
    assert verification.grant(UUID(session_id)) is None


async def test_expired_grant_keeps_the_pending_order_for_reverification(
    client, provider, verification, clock
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True

    clock.advance(VERIFICATION_TTL_SECONDS + 1)
    provider.script_extraction(order_lookup())
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert verification.pending_order(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert verification.grant(UUID(session_id)) is None


async def test_five_failures_lock_out_until_reset(
    client, provider, verification, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    saved_bytes = (session_dir / f"{session_id}.json").read_bytes()

    for _ in range(MAX_VERIFICATION_ATTEMPTS):
        response = await verify(client, session_id, code=WRONG_CODE)
        assert response.status_code == 200
        assert response.json()["error"] == "verification_failed"

    locked = await verify(client, session_id, code=WRONG_CODE)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "too_many_verification_attempts"
    assert locked.json()["error"]["message"] == text(
        "errors.too_many_verification_attempts", "en"
    )

    correct_but_locked = await verify(client, session_id)
    assert correct_but_locked.status_code == 429
    assert correct_but_locked.json()["error"]["code"] == (
        "too_many_verification_attempts"
    )
    assert verification.grant(UUID(session_id)) is None
    assert (session_dir / f"{session_id}.json").read_bytes() == saved_bytes


async def test_changing_the_order_cannot_bypass_the_failure_lockout(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)

    for _ in range(MAX_VERIFICATION_ATTEMPTS):
        assert (await verify(client, session_id, code=WRONG_CODE)).status_code == 200
    assert (await verify(client, session_id)).status_code == 429

    provider.script_extraction(
        make_extraction(candidates={"order_number": OTHER_ORDER})
    )
    changed = await send_turn(client, session_id, "Use order NS-000999.", 2)
    assert changed.status_code == 200
    assert changed.json()["slots"]["order_number"] == OTHER_ORDER
    assert verification.grant(UUID(session_id)) is None

    still_locked = await verify(client, session_id)

    assert still_locked.status_code == 429
    assert still_locked.json()["error"]["code"] == (
        "too_many_verification_attempts"
    )


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
    client, provider, verification, order_api
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
    assert order_api.order_gets() == []


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
    client, provider, verification, order_api
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
    assert order_api.order_gets() == []


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


async def test_completed_session_verification_is_rejected(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    created = await send_turn(
        client,
        session_id,
        "My order NS-000123 arrived broken. It is a damaged item and high urgency.",
        0,
    )
    assert created.status_code == 200
    assert created.json()["status"] == "awaiting_confirmation"
    confirmed = await client.post(
        f"/api/sessions/{session_id}/confirm",
        json={
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "review_revision": 1,
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "completed"

    response = await verify(client, session_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_completed"


async def test_verification_endpoint_rejects_malformed_or_unknown_sessions(
    client, provider
):
    unknown = await verify(client, str(uuid4()))
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "session_not_found"

    session_id = await create_session(client)
    malformed = await client.post(
        f"/api/sessions/{session_id}/order-verification",
        json={"delivery_postcode": "SW1A 1AA", "verification_code": "246810", "x": 1},
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_request"

    wrong_type = await client.post(
        f"/api/sessions/{session_id}/order-verification",
        json={"delivery_postcode": "SW1A 1AA", "verification_code": 246810},
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "invalid_request"

    empty = await client.post(
        f"/api/sessions/{session_id}/order-verification", json={}
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_request"


async def test_postcode_code_and_verified_credentials_are_never_persisted_or_logged(
    client, provider, verification, session_dir, caplog
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)

    caplog.set_level("INFO")
    await verify(client, session_id, postcode=" SW1A 1AA ", code=WRONG_CODE)
    await verify(client, session_id, postcode=" sw1a 1aa ")

    saved = read_session(session_dir, session_id)
    serialized = json.dumps(saved)
    for secret in (FIXTURE_POSTCODE, "SW1A", '"123456"', DEMO_VERIFICATION_CODE):
        assert secret not in serialized
    assert '"delivery_postcode"' not in serialized
    assert '"verification_code"' not in serialized
    assert '"verified_order_number"' not in serialized
    assert saved["revision"] == 2
    for secret in (FIXTURE_POSTCODE, "SW1A", '"123456"', DEMO_VERIFICATION_CODE):
        assert secret not in caplog.text
    for _, context in provider.calls:
        if not hasattr(context, "model_dump"):
            continue
        dump = json.dumps(context.model_dump(mode="json"))
        assert FIXTURE_POSTCODE not in dump
        assert '"123456"' not in dump
        assert DEMO_VERIFICATION_CODE not in dump


async def test_restart_clears_the_verification_grant(
    settings, provider, session_dir, verification, order_api
):
    first_app = create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        order_api_client=OrderApiClient(order_api.base_url),
    )
    async with running_client(first_app) as first_client:
        session_id = await create_session(first_client)
        provider.script_extraction(order_lookup())
        await send_turn(first_client, session_id, ORDER_STATUS_Q, 0)
        provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
        await send_turn(first_client, session_id, ORDER_NUMBER_TEXT, 1)
        assert (await verify(first_client, session_id)).json()["verified"] is True

    fresh_verification = VerificationRegistry(clock=FakeClock())
    fresh_api = LocalOrderApiServer()
    fresh_api.start()
    try:
        second_app = create_app(
            settings=settings,
            provider=provider,
            verification_registry=fresh_verification,
            order_api_client=OrderApiClient(fresh_api.base_url),
        )
        async with running_client(second_app) as second_client:
            provider.script_extraction(
                order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
            )
            response = await send_turn(second_client, session_id, ORDER_STATUS_Q, 2)

            assert response.status_code == 200
            assert response.json()["reply"] == VERIFICATION_PROMPT
            assert response.json()["order_verification_required"] is True
            assert fresh_verification.grant(UUID(session_id)) is None
            assert fresh_api.order_gets() == []
    finally:
        fresh_api.stop()


async def test_spanish_order_lookup_prompts_in_spanish(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(language="es"))

    response = await send_turn(client, session_id, "¿Dónde está mi pedido?", 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply_language"] == "es"
    assert body["reply"] == text("auth.order_number_prompt", "es")
    assert body["order_verification_required"] is False


async def test_ordinary_turn_response_defaults_order_verification_required_to_false(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(make_extraction(intent="social"))

    response = await send_turn(client, session_id, "Thanks.", 0)

    assert response.status_code == 200
    assert response.json()["order_verification_required"] is False
    assert persisted_response(response.json())["order_verification_required"] is False


async def test_invalid_order_candidate_gets_the_existing_invalid_order_reply(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(order_number="NS-12345"))

    response = await send_turn(client, session_id, "Where is my order?", 0)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == text("intake.invalid_order", "en")
    assert body["order_verification_required"] is False
    assert verification.pending_order(UUID(session_id)) is None
    assert verification.grant(UUID(session_id)) is None


async def test_bare_digits_and_lowercase_order_candidates_are_normalized(
    client, provider, verification
):
    bare = await create_session(client)
    provider.script_extraction(order_lookup(order_number="000123"))
    bare_response = await send_turn(client, bare, "Where is my order?", 0)
    assert bare_response.json()["reply"] == VERIFICATION_PROMPT
    assert verification.pending_order(UUID(bare)) == FIXTURE_ORDER_NUMBER

    lowercase = await create_session(client)
    provider.script_extraction(order_lookup(order_number="ns-000123"))
    lowercase_response = await send_turn(client, lowercase, "Where is my order?", 0)
    assert lowercase_response.json()["reply"] == VERIFICATION_PROMPT
    assert verification.pending_order(UUID(lowercase)) == FIXTURE_ORDER_NUMBER


async def test_pending_order_is_reused_when_a_followup_omits_the_number(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    assert (
        await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    ).json()["reply"] == ORDER_NUMBER_PROMPT
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    assert (
        await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    ).json()["reply"] == VERIFICATION_PROMPT

    provider.script_extraction(order_lookup())
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert verification.pending_order(UUID(session_id)) == FIXTURE_ORDER_NUMBER


async def test_verified_order_without_a_tool_request_does_not_call_the_api(
    client, provider, order_api
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True

    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == text("auth.verified", "en")
    assert body["order_verification_required"] is False
    assert order_api.order_gets() == []


async def test_policy_faq_between_verification_and_lookup_preserves_grant(
    client, provider, verification, order_api
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    await send_turn(client, session_id, ORDER_STATUS_Q, 0)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
    assert (await verify(client, session_id)).json()["verified"] is True

    provider.script_extraction(make_extraction(intent="question", has_question=True))
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )
    faq = await send_turn(client, session_id, RETURN_Q, 2)

    assert faq.status_code == 200
    assert faq.json()["reply"] == RETURN_ANSWER_TEXT
    assert [citation["source_id"] for citation in faq.json()["citations"]] == [
        "faq-returns"
    ]
    assert faq.json()["order_verification_required"] is False
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert order_api.order_gets() == []

    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    lookup = await send_turn(client, session_id, ORDER_STATUS_Q, 3)

    assert lookup.status_code == 200
    assert lookup.json()["reply"] == ORDER_RESULT
    assert order_api.order_gets() == [FIXTURE_ORDER_NUMBER]


async def test_lookup_during_review_preserves_summary_and_token(
    client, provider, verification
):
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    review = await send_turn(
        client,
        session_id,
        "My order NS-000123 arrived broken. It is a damaged item and high urgency.",
        0,
    )
    assert review.status_code == 200
    summary = build_summary(
        Slots(
            order_number=FIXTURE_ORDER_NUMBER,
            category="damaged_item",
            description="My order arrived broken",
            urgency="high",
        ),
        "en",
    )
    assert review.json()["summary"] == summary

    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 1)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert body["status"] == "awaiting_confirmation"
    assert body["summary"] == summary
    assert body["pending_review_revision"] == 1
    assert verification.grant(UUID(session_id)) is None


async def test_missing_fixture_record_clears_the_grant_and_fails_generically(
    settings, provider
):
    empty_registry = VerificationRegistry(clock=FakeClock())
    empty_api = LocalOrderApiServer(orders={})
    empty_api.start()
    try:
        app = create_app(
            settings=settings,
            provider=provider,
            verification_registry=empty_registry,
            order_api_client=OrderApiClient(empty_api.base_url),
        )
        async with running_client(app) as client:
            session_id = await create_session(client)
            provider.script_extraction(order_lookup())
            await send_turn(client, session_id, ORDER_STATUS_Q, 0)
            provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
            await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)
            assert (await verify(client, session_id)).json()["verified"] is True
            assert empty_registry.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER

            provider.script_extraction(
                order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
            )
            response = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

            assert response.status_code == 200
            body = response.json()
            assert body["reply"] == text("auth.verification_failed", "en")
            assert body["order_verification_required"] is False
            assert empty_registry.grant(UUID(session_id)) is None
    finally:
        empty_api.stop()


async def test_spanish_verified_lookup_uses_the_localized_status(
    client, provider
):
    spanish_result = render(
        "auth.order_result",
        "es",
        {
            "order_number": FIXTURE_ORDER_NUMBER,
            "first_name": "Casey",
            "item_names": "Electricity and gas supply",
            "status": text("auth.status_active", "es"),
        },
    )
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(language="es"))
    assert (
        await send_turn(client, session_id, "¿Dónde está mi pedido?", 0)
    ).json()["reply"] == text("auth.order_number_prompt", "es")
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, language="es")
    )
    assert (
        await send_turn(client, session_id, "El pedido es NS-000123.", 1)
    ).json()["reply"] == text("auth.verification_prompt", "es")
    assert (await verify(client, session_id)).json()["verified"] is True

    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, language="es", tool=True)
    )
    response = await send_turn(client, session_id, "¿Dónde está mi pedido?", 2)

    assert response.status_code == 200
    assert response.json()["reply"] == spanish_result
    assert "Activa" in response.json()["reply"]


async def test_frustrated_order_lookup_hands_off_before_verification_is_started(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(
        order_lookup(
            order_number=FIXTURE_ORDER_NUMBER,
            language="es",
            frustration="present",
        )
    )

    response = await send_turn(client, session_id, "¿Dónde está mi pedido?", 0)

    assert response.status_code == 200
    assert response.json()["reply"] == text("frustration_handoff", "es")
    assert response.json()["status"] == "handed_off"
    assert response.json()["order_verification_required"] is False
    assert provider.methods() == ["extract"]


async def test_spanish_invalid_order_uses_the_spanish_invalid_order_reply(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(
        order_lookup(order_number="NS-12345", language="es")
    )

    response = await send_turn(client, session_id, "¿Dónde está mi pedido?", 0)

    assert response.status_code == 200
    assert response.json()["reply"] == text("intake.invalid_order", "es")


async def test_spanish_verified_order_without_tool_uses_spanish_verified(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(language="es"))
    await send_turn(client, session_id, "¿Dónde está mi pedido?", 0)
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, language="es")
    )
    await send_turn(client, session_id, "El pedido es NS-000123.", 1)
    assert (await verify(client, session_id)).json()["verified"] is True

    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, language="es")
    )
    response = await send_turn(client, session_id, "¿Dónde está mi pedido?", 2)

    assert response.status_code == 200
    assert response.json()["reply"] == text("auth.verified", "es")


async def test_spanish_missing_fixture_record_uses_spanish_failure(
    settings, provider
):
    empty_registry = VerificationRegistry(clock=FakeClock())
    empty_api = LocalOrderApiServer(orders={})
    empty_api.start()
    try:
        app = create_app(
            settings=settings,
            provider=provider,
            verification_registry=empty_registry,
            order_api_client=OrderApiClient(empty_api.base_url),
        )
        async with running_client(app) as client:
            session_id = await create_session(client)
            provider.script_extraction(order_lookup(language="es"))
            await send_turn(client, session_id, "¿Dónde está mi pedido?", 0)
            provider.script_extraction(
                order_lookup(order_number=FIXTURE_ORDER_NUMBER, language="es")
            )
            await send_turn(client, session_id, "El pedido es NS-000123.", 1)
            assert (await verify(client, session_id)).json()["verified"] is True

            provider.script_extraction(
                order_lookup(order_number=FIXTURE_ORDER_NUMBER, language="es", tool=True)
            )
            response = await send_turn(client, session_id, "¿Dónde está mi pedido?", 2)

            assert response.status_code == 200
            assert response.json()["reply"] == text("auth.verification_failed", "es")
    finally:
        empty_api.stop()


async def test_order_lookup_at_the_collection_turn_limit_is_rejected(
    client, provider, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    review = await send_turn(
        client,
        session_id,
        "My order NS-000123 arrived broken. It is a damaged item and high urgency.",
        0,
    )
    assert review.status_code == 200
    assert review.json()["status"] == "awaiting_confirmation"

    path = session_dir / f"{session_id}.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["collection_turn_count"] = 50
    path.write_text(json.dumps(saved), encoding="utf-8")

    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    response = await send_turn(client, session_id, ORDER_STATUS_Q, 1)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "turn_limit"
    assert response.json()["error"]["message"] == text("errors.turn_limit", "en")


# the private Order API is a real process boundary, not an in-gateway
# object. These cases stop and restart the shipped Uvicorn server on the same
# origin and prove safe outage errors plus recovery without session mutation.


async def test_order_api_outage_during_verification_is_safe_and_recovers(
    client, provider, verification, order_api, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    assert (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).status_code == 200
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    assert (await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)).status_code == 200
    before = (session_dir / f"{session_id}.json").read_bytes()

    order_api.stop()
    down = await verify(client, session_id)

    assert down.status_code == 503
    assert down.json()["error"]["code"] == "order_api_unavailable"
    assert down.json()["error"]["message"] == text("errors.order_api_unavailable", "en")
    assert down.json()["error"]["retryable"] is True
    assert (session_dir / f"{session_id}.json").read_bytes() == before
    assert verification.grant(UUID(session_id)) is None
    assert verification.failures(UUID(session_id)) == 0
    assert FIXTURE_POSTCODE not in down.text
    assert DEMO_VERIFICATION_CODE not in down.text

    order_api.restart()
    recovered = await verify(client, session_id)

    assert recovered.status_code == 200
    assert recovered.json() == {"verified": True, "error": None}
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert order_api.verification_posts()[-1] == {
        "order_number": FIXTURE_ORDER_NUMBER,
        "delivery_postcode": FIXTURE_POSTCODE,
        "verification_code": DEMO_VERIFICATION_CODE,
    }


async def test_order_api_outage_during_lookup_preserves_the_grant_and_recovers(
    client, provider, verification, order_api, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup())
    assert (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).status_code == 200
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    assert (await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)).status_code == 200
    assert (await verify(client, session_id)).json()["verified"] is True
    before = (session_dir / f"{session_id}.json").read_bytes()

    order_api.stop()
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    down = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert down.status_code == 503
    assert down.json()["error"]["code"] == "order_api_unavailable"
    assert down.json()["error"]["message"] == text("errors.order_api_unavailable", "en")
    assert (session_dir / f"{session_id}.json").read_bytes() == before
    assert verification.grant(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    assert "Casey" not in down.text

    order_api.restart()
    provider.script_extraction(
        order_lookup(order_number=FIXTURE_ORDER_NUMBER, tool=True)
    )
    recovered = await send_turn(client, session_id, ORDER_STATUS_Q, 2)

    assert recovered.status_code == 200
    assert recovered.json()["reply"] == ORDER_RESULT
    assert recovered.json()["order_verification_required"] is False
    assert order_api.order_gets() == [FIXTURE_ORDER_NUMBER]


async def test_gateway_origin_does_not_expose_the_private_order_api(client):
    for method, path in (
        ("POST", "/internal/v1/order-verifications"),
        ("GET", f"/internal/v1/orders/{FIXTURE_ORDER_NUMBER}"),
        ("GET", "/internal/v1/orders"),
    ):
        response = await client.request(method, path)

        assert response.status_code == 404
        assert "Casey" not in response.text
        assert "Electricity and gas supply" not in response.text


class _FaultyOrderApiServer:
    """Real loopback Order API that stalls or returns unusable bodies (OA-I07)."""

    SLOW_SECONDS = 2.0
    STARTUP_SECONDS = 10.0
    SHUTDOWN_JOIN_SECONDS = 10.0

    def __init__(self) -> None:
        self.mode = "malformed"
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        self._socket = sock
        server = uvicorn.Server(
            uvicorn.Config(
                self._build_app(), log_level="warning", lifespan="on", access_log=False
            )
        )
        self._server = server
        thread = threading.Thread(
            target=lambda: server.run(sockets=[sock]),
            name=f"faulty-order-api-{self.port}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        deadline = time.monotonic() + self.STARTUP_SECONDS
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "faulty Order API did not become ready"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(self.SHUTDOWN_JOIN_SECONDS)
            assert not self._thread.is_alive(), "faulty Order API thread survived shutdown"
        if self._socket is not None:
            self._socket.close()

    def _build_app(self) -> FastAPI:
        owner = self
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/internal/v1/order-verifications")
        async def verify() -> JSONResponse:
            if owner.mode == "slow":
                await asyncio.sleep(owner.SLOW_SECONDS)
                return JSONResponse({"verified": True, "error": None})
            if owner.mode == "server_error":
                return JSONResponse({"error": "internal detail"}, status_code=500)
            return JSONResponse({"verified": "yes", "error": None})

        return app


@pytest.fixture
def faulty_order_api():
    server = _FaultyOrderApiServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.mark.parametrize(
    "mode,status,code",
    [
        pytest.param("slow", 504, ORDER_API_TIMEOUT, id="OA-I07-timeout"),
        pytest.param("malformed", 502, INVALID_ORDER_API_RESPONSE, id="OA-I07-malformed"),
        pytest.param("server_error", 502, INVALID_ORDER_API_RESPONSE, id="OA-I07-5xx"),
    ],
)
async def test_unusable_order_api_replies_map_to_fixed_safe_errors(
    mode,
    status,
    code,
    faulty_order_api,
    settings,
    provider,
    verification,
    session_dir,
):
    """OA-I07: slow/malformed/5xx Order API replies never reach the browser raw."""

    faulty_order_api.mode = mode
    app = create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        order_api_client=OrderApiClient(faulty_order_api.base_url, timeout_seconds=0.2),
    )
    async with running_client(app) as client:
        session_id = await create_session(client)
        provider.script_extraction(order_lookup())
        assert (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).status_code == 200
        provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
        assert (await send_turn(client, session_id, ORDER_NUMBER_TEXT, 1)).status_code == 200
        before = (session_dir / f"{session_id}.json").read_bytes()

        response = await verify(client, session_id)

        assert response.status_code == status
        error = response.json()["error"]
        assert error["code"] == code
        assert error["message"] == text(f"errors.{code}", "en")
        assert error["retryable"] is True
        assert (session_dir / f"{session_id}.json").read_bytes() == before
        assert verification.grant(UUID(session_id)) is None
        assert verification.failures(UUID(session_id)) == 0
        for leaked in (
            faulty_order_api.base_url,
            "127.0.0.1",
            str(faulty_order_api.port),
            FIXTURE_POSTCODE,
            DEMO_VERIFICATION_CODE,
            "internal detail",
        ):
            assert leaked not in response.text
