"""Unit tests for the SQLite technician availability and booking store."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from backend_api.bookings import (
    ALREADY_BOOKED,
    AVAILABILITY_PATH,
    SLOT_UNAVAILABLE,
    BookingRefused,
    BookingStore,
)

MONDAY_MORNING = datetime(2026, 11, 2, 8, 0)
FIRST_WINDOW = "2026-11-02T09:00"


@pytest.fixture
def store_at():
    """A factory for stores; every one it hands out is closed when the test ends."""
    opened: list[BookingStore] = []

    def make(
        moment: datetime = MONDAY_MORNING, path: str | None = None
    ) -> BookingStore:
        store = BookingStore(path, clock=lambda: moment)
        opened.append(store)
        return store

    yield make
    for store in opened:
        store.close()


def test_open_slots_are_the_next_six_windows_earliest_first(store_at) -> None:
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


def test_windows_already_started_are_not_offered(store_at) -> None:
    slots = store_at(datetime(2026, 11, 2, 12, 0)).open_slots()

    assert slots[0]["starts_at"] == "2026-11-02T13:00"


def test_weekends_are_skipped(store_at) -> None:
    friday_evening = datetime(2026, 11, 6, 16, 0)

    assert store_at(friday_evening).open_slots()[0]["starts_at"] == "2026-11-09T09:00"


def test_booking_reserves_the_window_and_returns_a_reference(store_at) -> None:
    store = store_at()

    appointment = store.book("NS-000123", FIRST_WINDOW)

    assert appointment["starts_at"] == FIRST_WINDOW
    assert appointment["ends_at"] == "2026-11-02T11:00"
    assert appointment["reference"].startswith("BK-")
    assert store.appointment("NS-000123") == appointment
    assert FIRST_WINDOW not in [slot["starts_at"] for slot in store.open_slots()]


def test_an_account_can_hold_only_one_visit(store_at) -> None:
    store = store_at()
    store.book("NS-000123", FIRST_WINDOW)

    with pytest.raises(BookingRefused) as refused:
        store.book("NS-000123", "2026-11-02T11:00")

    assert refused.value.code == ALREADY_BOOKED


@pytest.mark.parametrize(
    "starts_at",
    ["2026-11-02T09:00", "2026-11-07T09:00", "2026-11-02T10:00", "nonsense"],
)
def test_taken_missing_or_started_windows_are_refused(
    store_at, starts_at: str, tmp_path
) -> None:
    path = str(tmp_path / "bookings.db")
    store_at(datetime(2026, 11, 2, 8, 0), path=path).book("NS-000999", FIRST_WINDOW)
    store = store_at(datetime(2026, 11, 2, 9, 30), path=path)

    with pytest.raises(BookingRefused) as refused:
        store.book("NS-000123", starts_at)

    assert refused.value.code == SLOT_UNAVAILABLE


def test_bookings_survive_a_restart_and_the_top_up_keeps_them(
    store_at, tmp_path
) -> None:
    path = str(tmp_path / "bookings.db")
    booked = store_at(path=path).book("NS-000123", FIRST_WINDOW)

    reopened = store_at(datetime(2026, 11, 3, 8, 0), path=path)

    assert reopened.appointment("NS-000123") == booked
    assert FIRST_WINDOW not in [slot["starts_at"] for slot in reopened.open_slots()]
    assert reopened.open_slots()[-1]["starts_at"] > "2026-11-03T11:00"


def test_two_connections_to_one_file_cannot_take_the_same_window(
    store_at, tmp_path
) -> None:
    path = str(tmp_path / "bookings.db")
    first, second = store_at(path=path), store_at(path=path)

    first.book("NS-000123", FIRST_WINDOW)
    with pytest.raises(BookingRefused) as refused:
        second.book("NS-000999", FIRST_WINDOW)

    assert refused.value.code == SLOT_UNAVAILABLE


def test_a_booking_reference_is_bk_and_eight_uppercase_hex_characters(store_at) -> None:
    appointment = store_at().book("NS-000123", FIRST_WINDOW)

    assert re.fullmatch(r"BK-[0-9A-F]{8}", appointment["reference"])


def test_windows_are_seeded_for_every_weekday_of_the_configured_days_ahead(
    store_at, tmp_path
) -> None:
    path = str(tmp_path / "bookings.db")
    store_at(path=path)
    config = json.loads(AVAILABILITY_PATH.read_text("utf-8"))
    days = [
        MONDAY_MORNING + timedelta(days=offset)
        for offset in range(config["days_ahead"] + 1)
    ]
    expected = {day.strftime("%Y-%m-%d") for day in days if day.weekday() < 5}

    with sqlite3.connect(path) as db:
        seeded = {
            row[0]
            for row in db.execute("SELECT DISTINCT substr(starts_at, 1, 10) FROM slots")
        }
        windows = db.execute("SELECT COUNT(*) FROM slots").fetchone()[0]

    assert seeded == expected
    assert windows == len(expected) * len(config["windows"])


def test_the_database_folder_is_created_when_it_does_not_exist(
    store_at, tmp_path
) -> None:
    path = tmp_path / "nested" / "deeper" / "bookings.db"

    store_at(path=str(path))

    assert path.exists()


def test_a_second_visit_that_gets_past_the_check_is_still_refused_by_the_database(
    store_at, tmp_path
) -> None:
    store = store_at(path=str(tmp_path / "bookings.db"))
    store.book("NS-000123", FIRST_WINDOW)
    # a competing request that did not see the first booking yet
    store.appointment = lambda order_number: None

    with pytest.raises(BookingRefused) as refused:
        store.book("NS-000123", "2026-11-02T11:00")

    assert refused.value.code == ALREADY_BOOKED
