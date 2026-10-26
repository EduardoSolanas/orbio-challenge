"""SQLite technician availability and bookings for the Backend API.

One table holds every visit window. The database enforces the two booking rules
itself: a window is a primary key and is claimed by an UPDATE that only matches a
free one, and a unique index allows each order a single visit, so two customers
can never take the same window even across processes. The table is topped up
with weekday windows for the next two weeks at every start-up and existing
bookings are never touched. All data is fictional demo data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

from shared.models import ALREADY_BOOKED, SLOT_UNAVAILABLE

TIME_FORMAT = "%Y-%m-%dT%H:%M"
AVAILABILITY_PATH = Path(__file__).parent / "availability.json"
OFFER_LIMIT = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    starts_at TEXT PRIMARY KEY,
    ends_at TEXT NOT NULL,
    order_number TEXT,
    reference TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_visit_per_order
    ON slots (order_number) WHERE order_number IS NOT NULL;
"""


class BookingRefused(Exception):
    """The booking did not happen; ``code`` is the wire error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BookingStore:
    """Slots keyed by start time; ``path=None`` keeps them in memory only."""

    def __init__(
        self,
        path: str | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._clock = clock
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The service runs its handlers on one event loop and never awaits inside
        # the store, so one shared connection is safe.
        self._db = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._seed()

    def close(self) -> None:
        self._db.close()

    def _seed(self) -> None:
        """Add the weekday windows from ``availability.json`` for the coming days."""

        config = json.loads(AVAILABILITY_PATH.read_text("utf-8"))
        first = self._clock()
        rows = []
        for offset in range(config["days_ahead"] + 1):
            date = first + timedelta(days=offset)
            if date.weekday() >= 5:
                continue
            day = date.strftime("%Y-%m-%d")
            rows.extend(
                (f"{day}T{start}", f"{day}T{end}") for start, end in config["windows"]
            )
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO slots (starts_at, ends_at) VALUES (?, ?)", rows
            )

    def open_slots(self) -> list[dict[str, str]]:
        """The next free windows, earliest first."""

        rows = self._db.execute(
            "SELECT starts_at, ends_at FROM slots"
            " WHERE order_number IS NULL AND starts_at > ?"
            " ORDER BY starts_at LIMIT ?",
            (self._clock().strftime(TIME_FORMAT), OFFER_LIMIT),
        )
        return [{"starts_at": starts, "ends_at": ends} for starts, ends in rows]

    def appointment(self, order_number: str) -> dict[str, str] | None:
        row = self._db.execute(
            "SELECT starts_at, ends_at, reference FROM slots WHERE order_number = ?",
            (order_number,),
        ).fetchone()
        return (
            None
            if row is None
            else dict(zip(("starts_at", "ends_at", "reference"), row))
        )

    def book(
        self, order_number: str, starts_at: str, *, replace: bool = False
    ) -> dict[str, str]:
        """Reserve one open window for the order or raise ``BookingRefused``.

        With ``replace`` an existing visit moves to the new window and keeps its reference;
        the old window is freed and the new one claimed in one transaction, so a refused
        move leaves the visit where it was.
        """

        current = self.appointment(order_number)
        if current is not None and not replace:
            raise BookingRefused(ALREADY_BOOKED)
        if current is not None and current["starts_at"] == starts_at:
            return current
        reference = current["reference"] if current else f"BK-{uuid4().hex[:8].upper()}"
        try:
            with self._db:
                if current is not None:
                    self._db.execute(
                        "UPDATE slots SET order_number = NULL, reference = NULL"
                        " WHERE order_number = ?",
                        (order_number,),
                    )
                claimed = self._db.execute(
                    "UPDATE slots SET order_number = ?, reference = ?"
                    " WHERE starts_at = ? AND order_number IS NULL AND starts_at > ?",
                    (
                        order_number,
                        reference,
                        starts_at,
                        self._clock().strftime(TIME_FORMAT),
                    ),
                ).rowcount
                if not claimed:
                    # inside the transaction: the freed window is taken back on refusal
                    raise BookingRefused(SLOT_UNAVAILABLE)
        except (
            sqlite3.IntegrityError
        ):  # the unique index, should two bookings ever race
            raise BookingRefused(ALREADY_BOOKED) from None
        booked = self.appointment(order_number)
        assert booked is not None
        return booked
