"""The window text a customer reads, and the fingerprint that recognises a repeated booking."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from gateway.conversation.replay import booking_digest
from gateway.conversation.visits import window_values
from gateway.schemas import BookingRequest

SESSION = uuid4()
COMMAND = BookingRequest(
    request_id=uuid4(), expected_revision=2, starts_at="2026-11-02T09:00"
)


def test_a_window_is_split_into_its_date_and_clock_times() -> None:
    assert window_values("2026-11-02T09:00", "2026-11-02T11:30") == {
        "date": "2026-11-02",
        "start": "09:00",
        "end": "11:30",
    }


def test_the_same_booking_has_the_same_digest() -> None:
    assert booking_digest(SESSION, COMMAND) == booking_digest(
        SESSION, COMMAND.model_copy()
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": UUID(int=1)},
        {"expected_revision": 3},
        {"starts_at": "2026-11-02T11:00"},
    ],
    ids=["request_id", "expected_revision", "starts_at"],
)
def test_a_different_request_has_a_different_digest(changes: dict) -> None:
    other = COMMAND.model_copy(update=changes)

    assert booking_digest(SESSION, other) != booking_digest(SESSION, COMMAND)


def test_the_same_request_for_another_session_has_a_different_digest() -> None:
    assert booking_digest(uuid4(), COMMAND) != booking_digest(SESSION, COMMAND)


def test_a_booking_and_a_turn_with_the_same_values_do_not_collide() -> None:
    """The digest names its endpoint, so a replay of one kind never matches the other."""

    from gateway.conversation.replay import confirm_digest
    from gateway.schemas import ConfirmRequest

    confirm = ConfirmRequest(
        request_id=COMMAND.request_id, expected_revision=2, review_revision=2
    )

    assert booking_digest(SESSION, COMMAND) != confirm_digest(SESSION, confirm)
