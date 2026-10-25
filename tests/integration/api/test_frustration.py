"""Frustration-to-simulated-handoff behaviour over real HTTP."""
from __future__ import annotations

import pytest

from gateway.localization import text
from tests.integration.support.api import make_extraction, read_session
from tests.integration.support.intake import start_session, turn_body


def script_turn(provider, extraction):
    provider.script_extraction(extraction)


async def send(client, provider, session_id, message, revision, extraction):
    script_turn(provider, extraction)
    return await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(message, revision),
    )


async def test_fr01_new_session_has_no_frustration_or_handoff(client, provider):
    created = await client.post("/api/sessions", json={})
    body = created.json()
    assert created.status_code == 201
    assert body["status"] == "collecting"
    assert body["frustration_active"] is False
    assert body["messages"][0]["frustration"] is None
    assert provider.methods() == []


@pytest.mark.parametrize(
    "label,expected_status,active",
    [
        ("none", "collecting", False),
        ("uncertain", "collecting", False),
        ("present", "handed_off", True),
    ],
)
async def test_fr02_only_clear_new_frustration_triggers_handoff(
    client, provider, label, expected_status, active
):
    session_id = await start_session(client)
    response = await send(
        client, provider, session_id, "I am unhappy with this issue.", 0,
        make_extraction(intent="unrelated", sentiment="negative", frustration=label),
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == expected_status
    assert body["frustration_active"] is active
    if active:
        assert body["reply"] == text("frustration_handoff", "en")
        assert body["citations"] == []
    assert provider.methods() == ["extract"]


async def test_fr03_frustrated_faq_hands_off_without_retrieval_or_slot_changes(
    client, provider, session_dir
):
    session_id = await start_session(client)
    response = await send(
        client, provider, session_id,
        "I am frustrated. What is a standing charge?", 0,
        make_extraction(
            intent="question", has_question=True,
            candidates={"order_number": "NS-000123"},
            sentiment="negative", frustration="present",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == text("frustration_handoff", "en")
    assert body["status"] == "handed_off" and body["citations"] == []
    assert body["slots"] == {
        "order_number": None, "category": None, "description": None, "urgency": None
    }
    assert provider.methods() == ["extract"]
    assert read_session(session_dir, session_id)["slots"] == body["slots"]


async def test_fr04_spanish_frustration_uses_spanish_handoff(client, provider):
    session_id = await start_session(client)
    response = await send(
        client, provider, session_id, "Estoy frustrado y quiero hablar con alguien.", 0,
        make_extraction(
            intent="unrelated", detected_language="es", sentiment="negative",
            frustration="present",
        ),
    )
    assert response.status_code == 200
    assert response.json()["reply_language"] == "es"
    assert response.json()["reply"] == text("frustration_handoff", "es")
    assert response.json()["status"] == "handed_off"


async def test_fr05_handoff_at_review_preserves_summary_without_confirmation(
    client, provider, session_dir
):
    session_id = await start_session(client)
    started = await send(
        client, provider, session_id, "My order NS-000123 arrived broken; damaged item; high urgency.", 0,
        make_extraction(candidates={
            "order_number": "NS-000123", "category": "damaged_item",
            "description": "My order arrived broken", "urgency": "high",
        }),
    )
    assert started.status_code == 200
    summary = started.json()["summary"]
    response = await send(
        client, provider, session_id, "I am still frustrated.", 1,
        make_extraction(intent="unrelated", sentiment="negative", frustration="present"),
    )
    assert response.status_code == 200
    body = response.json()
    saved = read_session(session_dir, session_id)
    assert body["status"] == saved["status"] == "handed_off"
    assert body["summary"] == saved["summary"] == summary
    assert saved["confirmation"] is None


async def test_fr06_handoff_during_verification_disables_verification_endpoint(
    client, provider
):
    session_id = await start_session(client)
    issue = await send(
        client, provider, session_id, "Order NS-000123 arrived broken.", 0,
        make_extraction(candidates={
            "order_number": "NS-000123", "description": "My order arrived broken"
        }),
    )
    assert issue.status_code == 200
    lookup = await send(
        client, provider, session_id, "What is my account status?", 1,
        make_extraction(intent="order_lookup", has_question=True),
    )
    assert lookup.status_code == 200 and lookup.json()["order_verification_required"] is True
    handoff = await send(
        client, provider, session_id, "I am really frustrated.", 2,
        make_extraction(intent="unrelated", sentiment="negative", frustration="present"),
    )
    assert handoff.status_code == 200
    blocked = await client.post(
        f"/api/sessions/{session_id}/order-verification",
        json={"delivery_postcode": "SW1A 1AA", "verification_code": "246810"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "session_handed_off"
    assert provider.methods() == ["extract", "extract", "extract"]


async def test_fr07_handed_off_session_rejects_followup_before_model_call(client, provider):
    session_id = await start_session(client)
    handoff = await send(
        client, provider, session_id, "I am frustrated.", 0,
        make_extraction(sentiment="negative", frustration="present"),
    )
    assert handoff.status_code == 200
    followup = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body("Continue", 1)
    )
    assert followup.status_code == 409
    assert followup.json()["error"]["code"] == "session_handed_off"
    assert provider.methods() == ["extract"]

