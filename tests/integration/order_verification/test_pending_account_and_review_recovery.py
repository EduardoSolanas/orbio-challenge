"""An unverified account keeps asking for verification, and a rejected review is recovered, not confirmed."""

from __future__ import annotations

import json

from backend_api.main import FIXTURE_ORDER_NUMBER
from gateway.localization import text
from tests.integration.order_verification.support import (
    ORDER_STATUS_Q,
    create_session,
    full_intake,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support.api import make_extraction, session_path

INTAKE_TEXT = (
    "My order NS-000123 arrived broken. It is a damaged item and high urgency."
)


async def test_an_intake_turn_while_an_account_waits_for_verification_keeps_the_form_open(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    asked = (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).json()
    assert asked["order_verification_required"] is True
    provider.script_extraction(
        make_extraction(candidates={"description": "My meter display is blank"})
    )

    during = (
        await send_turn(
            client, session_id, "My meter display is blank", asked["revision"]
        )
    ).json()
    assert during["order_verification_required"] is True
    assert (await verify(client, session_id)).json()["verified"] is True
    provider.script_extraction(make_extraction(candidates={"category": "damaged_item"}))
    after = (
        await send_turn(
            client, session_id, "It is an equipment issue", during["revision"]
        )
    ).json()

    assert after["order_verification_required"] is False


async def rejected_review(client, provider, session_dir, *, at_the_limit=False):
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    review = (await send_turn(client, session_id, INTAKE_TEXT, 0)).json()
    provider.script_extraction(make_extraction(intent="reject"))
    rejected = (await send_turn(client, session_id, "No", review["revision"])).json()
    assert rejected["reply"] == text("reject_prompt", "en")
    if at_the_limit:
        path = session_path(session_dir, session_id)
        saved = json.loads(path.read_text("utf-8"))
        saved["collection_turn_count"] = 50
        path.write_text(json.dumps(saved), "utf-8")
    return session_id, rejected["revision"]


async def test_a_plain_yes_after_a_rejection_shows_the_review_again_instead_of_confirming(
    client, provider, session_dir
):
    session_id, revision = await rejected_review(client, provider, session_dir)
    provider.script_extraction(make_extraction(intent="confirm"))

    body = (await send_turn(client, session_id, "Yes", revision)).json()

    assert body["status"] == "awaiting_confirmation"
    assert body["summary"] and "REQ-" not in body["reply"]


async def test_a_plain_yes_after_a_rejection_is_refused_at_the_turn_limit(
    client, provider, session_dir
):
    session_id, revision = await rejected_review(
        client, provider, session_dir, at_the_limit=True
    )
    provider.script_extraction(make_extraction(intent="confirm"))

    response = await send_turn(client, session_id, "Yes", revision)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "turn_limit"
