"""JSON-file technician availability and bookings for the Backend API.

One JSON file holds every visit window. The service runs as a single process and
a booking is plain synchronous code with no await, so two customers can never
take the same window; the file is replaced atomically on every change. The file
is topped up with weekday windows for the next two weeks at every start-up and
existing bookings are never touched. All data is fictional demo data.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

SLOT_UNAVAILABLE = "slot_unavailable"
ALREADY_BOOKED = "already_booked"
TIME_FORMAT = "%Y-%m-%dT%H:%M"
AVAILABILITY_PATH = Path(__file__).parent / "availability.json"
OFFER_LIMIT = 6


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
        self._path = Path(path) if path else None
        self._clock = clock
        self._slots: dict[str, dict] = {}
        if self._path is not None and self._path.exists():
            self._slots = {s["starts_at"]: s for s in json.loads(self._path.read_text("utf-8"))}
        self._seed()
        self._save()

    def _seed(self) -> None:
        """Add the weekday windows from ``availability.json`` for the coming days."""

        config = json.loads(AVAILABILITY_PATH.read_text("utf-8"))
        first = self._clock()
        for offset in range(config["days_ahead"] + 1):
            date = first + timedelta(days=offset)
            if date.weekday() >= 5:
                continue
            day = date.strftime("%Y-%m-%d")
            for start, end in config["windows"]:
                starts_at = f"{day}T{start}"
                self._slots.setdefault(
                    starts_at,
                    {
                        "starts_at": starts_at,
                        "ends_at": f"{day}T{end}",
                        "order_number": None,
                        "reference": None,
                    },
                )

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(self._slots.values(), key=lambda s: s["starts_at"])), "utf-8")
        os.replace(temporary, self._path)

    def open_slots(self) -> list[dict[str, str]]:
        """The next free windows, earliest first."""

        now = self._clock().strftime(TIME_FORMAT)
        free = sorted(
            s for s in self._slots if self._slots[s]["order_number"] is None and s > now
        )
        return [
            {"starts_at": s, "ends_at": self._slots[s]["ends_at"]} for s in free[:OFFER_LIMIT]
        ]

    def appointment(self, order_number: str) -> dict[str, str] | None:
        for slot in self._slots.values():
            if slot["order_number"] == order_number:
                return {k: slot[k] for k in ("starts_at", "ends_at", "reference")}
        return None

    def book(self, order_number: str, starts_at: str) -> dict[str, str]:
        """Reserve one open window for the order or raise ``BookingRefused``."""

        if self.appointment(order_number) is not None:
            raise BookingRefused(ALREADY_BOOKED)
        slot = self._slots.get(starts_at)
        if slot is None or slot["order_number"] is not None or starts_at <= self._clock().strftime(TIME_FORMAT):
            raise BookingRefused(SLOT_UNAVAILABLE)
        slot["order_number"] = order_number
        slot["reference"] = f"BK-{uuid4().hex[:8].upper()}"
        self._save()
        return {k: slot[k] for k in ("starts_at", "ends_at", "reference")}
