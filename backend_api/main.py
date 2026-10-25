"""Private Backend API service: fixed fictional fixture and credential verification.

This module is the only process that holds the demo order catalog and the demo
verification code. The chat gateway reaches it over the internal network with
`gateway.backend_client.BackendApiClient`; the browser never sees this service because
Compose publishes no host port for it and the gateway exposes no `/internal`
route. The catalog is fictional sample data, not real customer information.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend_api.bookings import BookingRefused, BookingStore
from shared.models import (
    VERIFICATION_FAILED,
    Appointment,
    Availability,
    BookingCommand,
    InstallationRecord,
    OrderRecord,
    OrderVerificationCommand,
    OrderVerificationResult,
    SlotOption,
)

DATA_PATH_ENV = "BACKEND_DATA_PATH"

NOT_FOUND = "not_found"
DEMO_VERIFICATION_CODE = "246810"
FIXTURE_ORDER_NUMBER = "NS-000123"
FIXTURE_POSTCODE = "SW1A 1AA"

_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def normalize_postcode(value: str) -> str:
    """Trim surrounding whitespace and ASCII-uppercase only; no fuzzy matching."""

    return value.strip().translate(_ASCII_UPPER)


def valid_verification_code(value: str) -> bool:
    """Exactly six ASCII digits; Unicode digits and surrounding spaces fail."""

    return len(value) == 6 and all("0" <= character <= "9" for character in value)


def fixture_orders() -> dict[str, OrderRecord]:
    """The fixed fictional catalog, built fresh so callers cannot mutate shared state."""

    return {
        FIXTURE_ORDER_NUMBER: OrderRecord(
            order_number=FIXTURE_ORDER_NUMBER,
            first_name="Casey",
            item_names=["Electricity and gas supply"],
            status="active",
            dispatch_date=None,
            delivery_date=None,
        )
    }


def credentials_match(
    order_number: str | None, delivery_postcode: str, verification_code: str
) -> bool:
    """All three demo values must match; every mismatch is indistinguishable."""

    return (
        order_number == FIXTURE_ORDER_NUMBER
        and normalize_postcode(delivery_postcode) == FIXTURE_POSTCODE
        and valid_verification_code(verification_code)
        and verification_code == DEMO_VERIFICATION_CODE
    )


def create_backend_api_app(
    orders: dict[str, OrderRecord] | None = None,
    bookings: BookingStore | None = None,
) -> FastAPI:
    """Build the private Backend API; `orders` and `bookings` are test seams."""

    catalog = fixture_orders() if orders is None else orders
    store = (
        BookingStore(os.environ.get(DATA_PATH_ENV)) if bookings is None else bookings
    )
    app = FastAPI(
        title="Northstar backend API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post("/internal/v1/order-verifications")
    async def verify(command: OrderVerificationCommand) -> JSONResponse:
        verified = credentials_match(
            command.order_number, command.delivery_postcode, command.verification_code
        )
        result = OrderVerificationResult(
            verified=verified,
            error=None if verified else VERIFICATION_FAILED,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/internal/v1/orders/{order_number}")
    async def lookup(order_number: str):
        record = catalog.get(order_number)
        if record is None:
            return JSONResponse({"error": NOT_FOUND}, status_code=404)
        return JSONResponse(record.model_copy(deep=True).model_dump(mode="json"))

    @app.get("/internal/v1/orders/{order_number}/installation")
    async def installation(order_number: str):
        if order_number not in catalog:
            return JSONResponse({"error": NOT_FOUND}, status_code=404)
        appointment = store.appointment(order_number)
        record = InstallationRecord(
            order_number=order_number,
            stage="awaiting_visit" if appointment is None else "visit_booked",
            appointment=None if appointment is None else Appointment(**appointment),
        )
        return JSONResponse(record.model_dump(mode="json"))

    @app.get("/internal/v1/availability")
    async def availability() -> JSONResponse:
        result = Availability(slots=[SlotOption(**slot) for slot in store.open_slots()])
        return JSONResponse(result.model_dump(mode="json"))

    @app.post("/internal/v1/bookings")
    async def book(command: BookingCommand) -> JSONResponse:
        if command.order_number not in catalog:
            return JSONResponse({"error": NOT_FOUND}, status_code=404)
        try:
            booked = store.book(command.order_number, command.starts_at)
        except BookingRefused as refused:
            return JSONResponse({"error": refused.code}, status_code=409)
        return JSONResponse(Appointment(**booked).model_dump(mode="json"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_backend_api_app()
