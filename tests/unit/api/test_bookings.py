"""Unit tests for the JSON-file technician availability and booking store."""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.api.bookings import ALREADY_BOOKED, SLOT_UNAVAILABLE, BookingRefused, BookingStore

MONDAY_MORNING = datetime(2026, 11, 2, 8, 0)
FIRST_WINDOW = "2026-11-02T09:00"


def store_at(moment: datetime = MONDAY_MORNING, path: str | None = None) -> BookingStore:
    return BookingStore(path, clock=lambda: moment)


def test_open_slots_are_the_next_six_windows_earliest_first() -> None:
    slots = store_at().open_slots()

    assert [slot["starts_at"] for slot in slots] == [
        "2026-11-02T09:00",
        "2026-11-02T11:00",
        "2026-11-02T13:00",
        "2026-11-02T15:00",
        "2026-11-03T09:00",
        "2026-11-03T11:00",
    ]
    assert slots[0] == {"starts_at": "2026-11-02T09:00", "ends_at": "2026-11-02T11:00"}


def test_windows_already_started_are_not_offered() -> None:
    slots = store_at(datetime(2026, 11, 2, 12, 0)).open_slots()

    assert slots[0]["starts_at"] == "2026-11-02T13:00"


def test_weekends_are_skipped() -> None:
    friday_evening = datetime(2026, 11, 6, 16, 0)

    assert store_at(friday_evening).open_slots()[0]["starts_at"] == "2026-11-09T09:00"


def test_booking_reserves_the_window_and_returns_a_reference() -> None:
    store = store_at()

    appointment = store.book("NS-000123", FIRST_WINDOW)

    assert appointment["starts_at"] == FIRST_WINDOW
    assert appointment["ends_at"] == "2026-11-02T11:00"
    assert appointment["reference"].startswith("BK-")
    assert store.appointment("NS-000123") == appointment
    assert FIRST_WINDOW not in [slot["starts_at"] for slot in store.open_slots()]


def test_an_account_can_hold_only_one_visit() -> None:
    store = store_at()
    store.book("NS-000123", FIRST_WINDOW)

    with pytest.raises(BookingRefused) as refused:
        store.book("NS-000123", "2026-11-02T11:00")

    assert refused.value.code == ALREADY_BOOKED


@pytest.mark.parametrize(
    "starts_at", ["2026-11-02T09:00", "2026-11-07T09:00", "2026-11-02T10:00", "nonsense"]
)
def test_taken_missing_or_started_windows_are_refused(starts_at: str) -> None:
    store = store_at(datetime(2026, 11, 2, 9, 30))
    store._slots["2026-11-02T09:00"]["order_number"] = "NS-000999"

    with pytest.raises(BookingRefused) as refused:
        store.book("NS-000123", starts_at)

    assert refused.value.code == SLOT_UNAVAILABLE


def test_bookings_survive_a_restart_and_the_top_up_keeps_them(tmp_path) -> None:
    path = str(tmp_path / "bookings.json")
    booked = store_at(path=path).book("NS-000123", FIRST_WINDOW)

    reopened = store_at(datetime(2026, 11, 3, 8, 0), path=path)

    assert reopened.appointment("NS-000123") == booked
    assert FIRST_WINDOW not in [slot["starts_at"] for slot in reopened.open_slots()]
    assert reopened.open_slots()[-1]["starts_at"] > "2026-11-03T11:00"
