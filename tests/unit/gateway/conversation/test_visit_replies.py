"""The text of an order summary and of a visit, and the booking offer when a visit exists."""

from __future__ import annotations

from types import SimpleNamespace

from gateway.conversation.engine import ConversationEngine
from gateway.conversation.visits import booking_offer, installation_text
from gateway.localization import text
from shared.models import Appointment, InstallationRecord, OrderRecord

APPOINTMENT = Appointment(
    starts_at="2026-11-02T09:00", ends_at="2026-11-02T11:00", reference="BK-0D8B3C88"
)
BOOKED = InstallationRecord(
    order_number="NS-000123", stage="visit_booked", appointment=APPOINTMENT
)


def test_an_order_summary_lists_every_item_separated_by_commas() -> None:
    order = OrderRecord(
        order_number="NS-000123",
        first_name="Casey",
        item_names=["Electricity supply", "Gas supply"],
        status="active",
        dispatch_date=None,
        delivery_date=None,
    )

    assert "Electricity supply, Gas supply" in ConversationEngine._order_summary(
        order, "en"
    )


def test_a_booked_visit_names_the_account_it_belongs_to() -> None:
    reply = installation_text("NS-000123", BOOKED, "en")

    assert "NS-000123" in reply and "BK-0D8B3C88" in reply


class Client:
    def __init__(self, record, slots=()):
        self._record, self._slots = record, slots

    async def installation(self, order_number):
        return self._record

    async def availability(self):
        return SimpleNamespace(slots=list(self._slots))


async def test_asking_to_book_with_a_visit_already_booked_reports_that_visit_for_the_account() -> (
    None
):
    reply, slots = await booking_offer(Client(BOOKED), "NS-000123", "en")

    assert reply == installation_text("NS-000123", BOOKED, "en")
    assert "NS-000123" in reply and slots == ()


async def test_with_no_windows_left_the_customer_is_told_so_in_their_language() -> None:
    reply, slots = await booking_offer(Client(None), "NS-000123", "es")

    assert reply == text("booking.no_slots", "es") != text("booking.no_slots", "en")
    assert slots == ()
