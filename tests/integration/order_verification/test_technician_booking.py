"""Installation status and technician booking through the gateway and Backend API.

The gateway app runs for real against the shipped Backend API served by Uvicorn
on a loopback port; only the AI provider is scripted. The model picks the intent,
the Backend API supplies every offered window and reference.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend_api.main import FIXTURE_ORDER_NUMBER
from gateway.localization import render, text
from tests.integration.order_verification.support import (
    VERIFICATION_PROMPT,
    create_session,
    order_lookup,
    send_turn,
    verify,
)
from tests.integration.support.api import make_extraction, read_session


def intent(name: str, *, order_number: str | None = None):
    return make_extraction(intent=name, candidates={"order_number": order_number})


async def verified_session(client, provider) -> tuple[str, int]:
    """A session whose first turn named the fixture account and passed verification."""

    session_id = await create_session(client)
    provider.script_extraction(intent("installation_status", order_number=FIXTURE_ORDER_NUMBER))
    first = await send_turn(client, session_id, "Where is my installation?", 0)
    assert first.json()["order_verification_required"] is True
    verified = (await verify(client, session_id)).json()
    assert verified["verified"] is True
    # Verifying answers the waiting request in the chat, which moves the revision on.
    return session_id, verified["turn"]["revision"]


async def book(client, session_id: str, revision: int, starts_at: str, request_id=None):
    return await client.post(
        f"/api/sessions/{session_id}/bookings",
        json={
            "request_id": str(request_id or uuid4()),
            "expected_revision": revision,
            "starts_at": starts_at,
        },
    )


async def offered_windows(client, provider, session_id: str, revision: int) -> tuple[list, int]:
    provider.script_extraction(intent("book_technician"))
    response = await send_turn(client, session_id, "Book a technician please", revision)
    body = response.json()
    assert body["reply"] == text("booking.choose", "en")
    return body["slot_options"], body["revision"]


async def test_installation_status_asks_for_verification_first(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(intent("installation_status", order_number=FIXTURE_ORDER_NUMBER))

    body = (await send_turn(client, session_id, "Installation status?", 0)).json()

    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert body["slot_options"] == []


async def test_the_flow_and_its_step_are_saved_with_the_session_and_end_with_the_booking(
    client, provider, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(intent("book_technician"))
    first = (await send_turn(client, session_id, "Book a technician", 0)).json()
    assert read_session(session_dir, session_id)["flow"] == {
        "name": "book_technician",
        "step": "ask_account",
    }

    provider.script_extraction(intent("book_technician", order_number=FIXTURE_ORDER_NUMBER))
    second = (await send_turn(client, session_id, FIXTURE_ORDER_NUMBER, first["revision"])).json()
    assert read_session(session_dir, session_id)["flow"]["step"] == "verify_account"

    turn = (await verify(client, session_id)).json()["turn"]
    # The windows are on offer; choosing one finishes the booking and the flow.
    assert read_session(session_dir, session_id)["flow"] is None
    booked = await book(client, session_id, turn["revision"], turn["slot_options"][0]["starts_at"])
    assert booked.status_code == 200
    assert read_session(session_dir, session_id)["flow"] is None
    assert second["order_verification_required"] is True


async def test_a_malformed_account_number_while_waiting_for_verification_is_corrected(
    client, provider, session_dir
):
    session_id = await create_session(client)
    provider.script_extraction(intent("book_technician", order_number=FIXTURE_ORDER_NUMBER))
    first = (await send_turn(client, session_id, "Book a technician", 0)).json()
    assert first["order_verification_required"] is True
    # Still waiting for the code, the customer sends another account, and it is malformed.
    provider.script_extraction(
        make_extraction(intent="provide_details", candidates={"order_number": "NS-12"})
    )

    response = await send_turn(client, session_id, "NS-12", first["revision"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == text("intake.invalid_order", "en")
    assert body["order_verification_required"] is False and body["slot_options"] == []
    assert read_session(session_dir, session_id)["flow"] == {"name": "book_technician", "step": "ask_account"}


async def test_a_bare_account_number_keeps_the_booking_request_going(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(intent("book_technician"))
    first = (await send_turn(client, session_id, "I would like to book a technician visit", 0)).json()
    assert first["reply"] == text("auth.order_number_prompt", "en")
    # The model sees "NS-000123" alone and calls it plain details, not a booking.
    provider.script_extraction(
        make_extraction(intent="provide_details", candidates={"order_number": FIXTURE_ORDER_NUMBER})
    )

    body = (await send_turn(client, session_id, FIXTURE_ORDER_NUMBER, first["revision"])).json()

    assert body["reply"] == VERIFICATION_PROMPT
    assert body["order_verification_required"] is True
    assert body["slots"]["category"] is None


async def test_verifying_shows_the_windows_for_the_booking_that_was_asked_for(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(intent("book_technician", order_number=FIXTURE_ORDER_NUMBER))
    first = (await send_turn(client, session_id, "Book a technician", 0)).json()

    verified = (await verify(client, session_id)).json()

    turn = verified["turn"]
    assert verified["verified"] is True
    assert turn["reply"] == text("booking.choose", "en")
    assert len(turn["slot_options"]) == 6
    assert turn["revision"] == first["revision"] + 1
    # The chat history keeps the answer.
    view = (await client.get(f"/api/sessions/{session_id}")).json()
    assert view["messages"][-1]["text"] == text("booking.choose", "en")
    assert view["revision"] == turn["revision"]


async def test_verifying_with_nothing_waiting_adds_no_reply(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(order_lookup(order_number=FIXTURE_ORDER_NUMBER))

    await send_turn(client, session_id, "Where is my order?", 0)
    verified = (await verify(client, session_id)).json()

    assert verified == {"verified": True, "error": None}


async def test_a_different_request_drops_the_waiting_booking(client, provider):
    session_id = await create_session(client)
    provider.script_extraction(intent("book_technician"))
    first = (await send_turn(client, session_id, "Book a technician", 0)).json()
    provider.script_extraction(make_extraction(intent="start_intake", candidates={"description": "Broken meter"}))
    second = (await send_turn(client, session_id, "Actually my meter is broken", first["revision"])).json()
    provider.script_extraction(
        make_extraction(intent="provide_details", candidates={"order_number": FIXTURE_ORDER_NUMBER})
    )

    body = (await send_turn(client, session_id, FIXTURE_ORDER_NUMBER, second["revision"])).json()

    # It went into the support request, not back to the booking.
    assert body["slots"]["order_number"] == FIXTURE_ORDER_NUMBER
    assert body["slots"]["description"] == "Broken meter"
    assert body["slot_options"] == []
    assert body["reply"] != VERIFICATION_PROMPT


async def test_installation_status_after_verification_says_a_visit_is_needed(client, provider):
    session_id, revision = await verified_session(client, provider)
    provider.script_extraction(intent("installation_status"))

    body = (await send_turn(client, session_id, "And the installation?", revision)).json()

    assert body["reply"] == render(
        "installation.awaiting_visit", "en", {"order_number": FIXTURE_ORDER_NUMBER}
    )


async def test_booking_offers_windows_and_the_chosen_one_is_booked(client, provider):
    session_id, revision = await verified_session(client, provider)
    windows, revision = await offered_windows(client, provider, session_id, revision)
    chosen = windows[1]

    response = await book(client, session_id, revision, chosen["starts_at"])

    assert response.status_code == 200
    body = response.json()
    assert len(windows) == 6
    assert body["reply"].startswith(f"Booked: a technician will visit on {chosen['starts_at'][:10]}")
    assert body["slot_options"] == []
    assert body["revision"] == revision + 1
    provider.script_extraction(intent("installation_status"))
    status = (await send_turn(client, session_id, "Installation status?", body["revision"])).json()
    assert "Booking reference: BK-" in status["reply"]
    assert chosen["starts_at"][:10] in status["reply"]


async def test_a_booked_account_is_shown_its_visit_instead_of_new_windows(client, provider):
    session_id, revision = await verified_session(client, provider)
    windows, revision = await offered_windows(client, provider, session_id, revision)
    booked = (await book(client, session_id, revision, windows[0]["starts_at"])).json()
    provider.script_extraction(intent("book_technician"))

    body = (await send_turn(client, session_id, "Book a technician", booked["revision"])).json()

    assert body["slot_options"] == []
    assert "Booking reference: BK-" in body["reply"]


async def test_booking_without_verification_is_refused(client):
    session_id = await create_session(client)

    response = await book(client, session_id, 0, "2030-01-07T09:00")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "verification_required"


async def test_an_unknown_window_is_refused_as_unavailable(client, provider):
    session_id, revision = await verified_session(client, provider)

    response = await book(client, session_id, revision, "2000-01-03T09:00")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_unavailable"


async def test_a_second_window_for_the_same_account_is_refused(client, provider):
    session_id, revision = await verified_session(client, provider)
    windows, revision = await offered_windows(client, provider, session_id, revision)
    booked = (await book(client, session_id, revision, windows[0]["starts_at"])).json()

    response = await book(client, session_id, booked["revision"], windows[1]["starts_at"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_booked"


async def test_repeating_a_booking_request_replays_without_a_second_backend_call(
    client, provider, backend_api
):
    session_id, revision = await verified_session(client, provider)
    windows, revision = await offered_windows(client, provider, session_id, revision)
    request_id = uuid4()

    first = await book(client, session_id, revision, windows[0]["starts_at"], request_id)
    second = await book(client, session_id, revision, windows[0]["starts_at"], request_id)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    posts = [r for r in backend_api.requests if r["path"] == "/internal/v1/bookings"]
    assert len(posts) == 1


async def test_a_retry_with_a_new_request_id_after_a_lost_response_still_succeeds(
    client, provider
):
    session_id, revision = await verified_session(client, provider)
    windows, revision = await offered_windows(client, provider, session_id, revision)
    first = await book(client, session_id, revision, windows[0]["starts_at"])

    retry = await book(client, session_id, first.json()["revision"], windows[0]["starts_at"])

    assert retry.status_code == 200
    assert retry.json()["reply"] == first.json()["reply"]


@pytest.mark.parametrize("body", [{}, {"starts_at": 5}, {"starts_at": "x" * 40}])
async def test_a_malformed_booking_body_is_invalid_request(client, body):
    session_id = await create_session(client)

    response = await client.post(
        f"/api/sessions/{session_id}/bookings",
        json={"request_id": str(uuid4()), "expected_revision": 0, **body},
    )

    assert response.status_code == 422
