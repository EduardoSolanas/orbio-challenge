"""Reply text for installation status and technician booking.

The model only chooses the intent; every time, date and reference below comes
from the Backend API, so a reply can never invent an appointment.
"""

from __future__ import annotations

from app.backend_api import Appointment, InstallationRecord
from app.backend_client import BackendApiClient
from app.localization import render, text
from app.schemas import Language, SlotOption


def window_values(starts_at: str, ends_at: str) -> dict[str, str]:
    """Split ``YYYY-MM-DDTHH:MM`` stamps into the date and clock parts of a reply."""

    return {"date": starts_at[:10], "start": starts_at[11:16], "end": ends_at[11:16]}


def visit_text(
    key: str, language: Language, appointment: Appointment, **extra: str
) -> str:
    values = window_values(appointment.starts_at, appointment.ends_at)
    return render(
        key, language, {**values, "reference": appointment.reference, **extra}
    )


def installation_text(
    order_number: str, record: InstallationRecord | None, language: Language
) -> str:
    if record is None or record.appointment is None:
        return render(
            "installation.awaiting_visit", language, {"order_number": order_number}
        )
    return visit_text(
        "installation.visit_booked",
        language,
        record.appointment,
        order_number=order_number,
    )


async def installation_reply(
    client: BackendApiClient, order_number: str, language: Language
) -> str:
    return installation_text(
        order_number, await client.installation(order_number), language
    )


async def booking_offer(
    client: BackendApiClient, order_number: str, language: Language
) -> tuple[str, tuple[SlotOption, ...]]:
    """The existing visit if there is one, else the open windows to choose from."""

    record = await client.installation(order_number)
    if record is not None and record.appointment is not None:
        return installation_text(order_number, record, language), ()
    slots = tuple((await client.availability()).slots)
    if not slots:
        return text("booking.no_slots", language), ()
    return text("booking.choose", language), slots
