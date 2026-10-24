"""Integration tests for order verification over the real Order API.

The gateway app, lifespan, routes, engine and JSON repository run against an
isolated temp store; only the external AI provider is scripted. The shipped
Order API is served by real Uvicorn on a loopback port, so every verification
and lookup crosses a genuine TCP/HTTP boundary through `OrderApiClient`: test order lookup."""

from __future__ import annotations

import json
from uuid import UUID

from app.localization import build_summary, render, text
from app.main import create_app
from app.order_api import FIXTURE_ORDER_NUMBER
from app.order_client import OrderApiClient
from app.schemas import KnowledgeAnswer, Slots
from app.verification import VerificationRegistry
from tests.integration.order_verification.support import (
    FakeClock,
    ORDER_NUMBER_PROMPT,
    ORDER_NUMBER_TEXT,
    ORDER_RESULT,
    ORDER_STATUS_Q,
    RETURN_ANSWER_TEXT,
    RETURN_Q,
    VERIFICATION_PROMPT,
    create_session,
    full_intake,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support_api import (
    make_extraction,
    persisted_response,
    running_client,
)
from tests.integration.support_order_api import LocalOrderApiServer


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
