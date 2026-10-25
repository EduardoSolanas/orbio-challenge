"""Integration tests for order verification over the real Backend API.

The gateway app, lifespan, routes, engine and JSON repository run against an
isolated temp store; only the external AI provider is scripted. The shipped
Backend API is served by real Uvicorn on a loopback port, so every verification
and lookup crosses a genuine TCP/HTTP boundary through `BackendApiClient`: test verification endpoint."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from gateway.main import create_app
from backend_api.main import DEMO_VERIFICATION_CODE, FIXTURE_ORDER_NUMBER, FIXTURE_POSTCODE
from gateway.backend_client import BackendApiClient
from gateway.conversation.verification import VerificationRegistry
from tests.integration.order_verification.support import (
    ORDER_NUMBER_TEXT,
    ORDER_STATUS_Q,
    VERIFICATION_PROMPT,
    WRONG_CODE,
    FakeClock,
    create_session,
    full_intake,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support.api import read_session, running_client
from tests.integration.support.backend_api import LocalBackendApiServer


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
    settings, provider, session_dir, verification, backend_api
):
    first_app = create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        backend_api_client=BackendApiClient(backend_api.base_url),
    )
    async with running_client(first_app) as first_client:
        session_id = await create_session(first_client)
        provider.script_extraction(order_lookup())
        await send_turn(first_client, session_id, ORDER_STATUS_Q, 0)
        provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
        await send_turn(first_client, session_id, ORDER_NUMBER_TEXT, 1)
        assert (await verify(first_client, session_id)).json()["verified"] is True

    fresh_verification = VerificationRegistry(clock=FakeClock())
    fresh_api = LocalBackendApiServer()
    fresh_api.start()
    try:
        second_app = create_app(
            settings=settings,
            provider=provider,
            verification_registry=fresh_verification,
            backend_api_client=BackendApiClient(fresh_api.base_url),
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
