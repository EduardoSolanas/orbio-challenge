"""What the flows must keep true around them: the pending review, the language, the turn limit."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from backend_api.main import FIXTURE_ORDER_NUMBER
from gateway.errors import app_error
from gateway.localization import missing_question, text
from tests.integration.order_verification.support import (
    ORDER_STATUS_Q,
    book,
    create_session,
    full_intake,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support.api import make_extraction, read_session, session_path

INTAKE_TEXT = (
    "My order NS-000123 arrived broken. It is a damaged item and high urgency."
)


def booking(*, language: str = "en", order_number: str | None = None):
    return make_extraction(
        intent="book_technician",
        candidates={"order_number": order_number},
        detected_language=language,
    )


# --- a booking detour leaves the pending review alone -----------------------------------


async def test_booking_in_the_middle_of_a_review_keeps_the_review(
    client, provider, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    review = (await send_turn(client, session_id, INTAKE_TEXT, 0)).json()
    assert review["status"] == "awaiting_confirmation" and review["summary"]
    provider.script_extraction(booking())
    asked = (
        await send_turn(client, session_id, "Book a technician", review["revision"])
    ).json()
    assert asked["order_verification_required"] is True

    turn = (await verify(client, session_id)).json()["turn"]
    booked = (
        await book(
            client, session_id, turn["revision"], turn["slot_options"][0]["starts_at"]
        )
    ).json()

    for body in (turn, booked):
        assert body["status"] == "awaiting_confirmation"
        assert body["pending_review_revision"] == review["pending_review_revision"]
        assert body["summary"] == review["summary"]
    saved = read_session(session_dir, session_id)
    assert saved["pending_review_revision"] == review["pending_review_revision"]
    assert saved["summary"] == review["summary"]


# --- the flow answers in the customer's language ----------------------------------------


async def test_the_whole_booking_flow_answers_in_spanish(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(
        booking(language="es", order_number=FIXTURE_ORDER_NUMBER)
    )
    asked = (
        await send_turn(client, session_id, "Quiero reservar un técnico", 0)
    ).json()
    assert (
        asked["reply"] == text("auth.verification_prompt", "es")
        and asked["reply_language"] == "es"
    )

    turn = (await verify(client, session_id)).json()["turn"]
    assert (
        turn["reply"] == text("booking.choose", "es") and turn["reply_language"] == "es"
    )

    booked = (
        await book(
            client, session_id, turn["revision"], turn["slot_options"][0]["starts_at"]
        )
    ).json()
    assert booked["reply"].startswith("Reservado:") and booked["reply_language"] == "es"


# --- fixed replies keep the language of the turn ----------------------------------------


@pytest.mark.parametrize(
    ("intent", "key"),
    [
        ("capability", "capability_reply"),
        ("unrelated", "capability"),
        ("confirm", "nothing_to_confirm"),
        ("reject", "no_review_to_reject"),
        ("social", "social_acknowledgment"),
        ("gratitude", "gratitude_acknowledgment"),
    ],
)
async def test_fixed_replies_come_in_spanish(client, provider, intent, key):
    session_id = await create_session(client)
    provider.script_extraction(make_extraction(intent=intent, detected_language="es"))

    reply = (await send_turn(client, session_id, "Hola", 0)).json()["reply"]

    assert reply == text(key, "es") != text(key, "en")


async def test_rejecting_a_review_in_spanish_asks_what_to_change_in_spanish(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    review = (await send_turn(client, session_id, INTAKE_TEXT, 0)).json()
    provider.script_extraction(make_extraction(intent="reject", detected_language="es"))

    reply = (await send_turn(client, session_id, "No", review["revision"])).json()[
        "reply"
    ]

    assert reply == text("reject_prompt", "es") != text("reject_prompt", "en")


async def test_a_question_asked_with_details_adds_the_suffix_in_spanish(
    client, provider
):
    session_id = await create_session(client)
    provider.script_extraction(
        make_extraction(
            intent="provide_details",
            has_question=True,
            candidates={"order_number": FIXTURE_ORDER_NUMBER},
            detected_language="es",
        )
    )

    reply = (
        await send_turn(
            client, session_id, "Mi cuenta es NS-000123, ¿hay algún plazo?", 0
        )
    ).json()["reply"]

    assert (
        reply
        == f"{missing_question('category', 'es')} {text('policy_question_suffix', 'es')}"
    )


# --- the turn limit applies to every path ------------------------------------------------


async def reach_the_turn_limit(client, provider, session_dir) -> tuple[str, int]:
    session_id = await create_session(client)
    provider.script_extraction(full_intake())
    review = (await send_turn(client, session_id, INTAKE_TEXT, 0)).json()
    path = session_path(session_dir, session_id)
    saved = json.loads(path.read_text("utf-8"))
    saved["collection_turn_count"] = 50
    path.write_text(json.dumps(saved), "utf-8")
    return session_id, review["revision"]


@pytest.mark.parametrize(
    "extraction",
    [
        booking(),
        make_extraction(intent="provide_details", detected_language="unsupported"),
    ],
    ids=["a_booking_flow_turn", "an_unsupported_language_turn"],
)
async def test_the_turn_limit_refuses_flow_and_unsupported_turns(
    client, provider, session_dir, extraction
):
    session_id, revision = await reach_the_turn_limit(client, provider, session_dir)
    provider.script_extraction(extraction)

    response = await send_turn(client, session_id, "Book a technician", revision)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "turn_limit"
    assert response.json()["error"]["message"] == text("errors.turn_limit", "en")


# --- a hand-off forgets the verification state --------------------------------------------


async def test_a_hand_off_forgets_the_pending_account(client, provider, app):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))
    asked = (await send_turn(client, session_id, ORDER_STATUS_Q, 0)).json()
    registry = app.state.engine.verification
    assert registry.pending_order(UUID(session_id)) == FIXTURE_ORDER_NUMBER
    provider.script_extraction(
        make_extraction(intent="unrelated", sentiment="negative", frustration="present")
    )

    handoff = (
        await send_turn(
            client, session_id, "I am really frustrated.", asked["revision"]
        )
    ).json()

    assert handoff["status"] == "handed_off"
    assert registry.pending_order(UUID(session_id)) is None


# --- verification and booking lock the customer's own session ---------------------------------


async def test_verification_and_booking_lock_the_session_they_belong_to(
    client, provider, app, monkeypatch
):
    locked: list[UUID] = []
    repository = app.state.engine.repository
    original = repository.locked

    def spy(session_id):
        locked.append(session_id)
        return original(session_id)

    monkeypatch.setattr(repository, "locked", spy)
    session_id = await create_session(client)
    provider.script_extraction(booking(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, "Book a technician", 0)
    locked.clear()

    turn = (await verify(client, session_id)).json()["turn"]
    await book(
        client, session_id, turn["revision"], turn["slot_options"][0]["starts_at"]
    )

    assert locked == [UUID(session_id), UUID(session_id)]


# --- a refusal that says "already booked" but has no visit to show -----------------------------


async def test_an_already_booked_refusal_with_no_visit_on_record_is_passed_on(
    client, provider, app, monkeypatch
):
    session_id = await create_session(client)
    provider.script_extraction(booking(order_number=FIXTURE_ORDER_NUMBER))
    await send_turn(client, session_id, "Book a technician", 0)
    turn = (await verify(client, session_id)).json()["turn"]

    class Refusing:
        async def book(self, order_number, starts_at, *, replace=False):
            raise app_error("already_booked")

        async def installation(self, order_number):
            return None

    monkeypatch.setattr(app.state.engine, "customer_data", Refusing())
    response = await book(
        client, session_id, turn["revision"], turn["slot_options"][0]["starts_at"]
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_booked"
