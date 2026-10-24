"""Integration tests for order verification over the real Order API.

The gateway app, lifespan, routes, engine and JSON repository run against an
isolated temp store; only the external AI provider is scripted. The shipped
Order API is served by real Uvicorn on a loopback port, so every verification
and lookup crosses a genuine TCP/HTTP boundary through `OrderApiClient`: test verification flow."""

from __future__ import annotations

import json
from uuid import UUID

from app.localization import text
from app.order_api import DEMO_VERIFICATION_CODE, FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE
from app.verification import MAX_VERIFICATION_ATTEMPTS, VERIFICATION_TTL_SECONDS
from tests.integration.order_verification.support import (
    ORDER_NUMBER_PROMPT,
    ORDER_NUMBER_TEXT,
    ORDER_RESULT,
    ORDER_STATUS_Q,
    OTHER_ORDER,
    VERIFICATION_PROMPT,
    WRONG_CODE,
    create_session,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support_api import make_extraction


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
