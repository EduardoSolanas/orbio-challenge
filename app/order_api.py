"""Private Order API service: fixed fictional fixture and credential verification.

This module is the only process that holds the demo order catalog and the demo
verification code. The chat gateway reaches it over the internal network with
`app.order_client.OrderApiClient`; the browser never sees this service because
Compose publishes no host port for it and the gateway exposes no `/internal`
route. The catalog is fictional sample data, not real customer information.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool, StrictStr, model_validator

from app.schemas import ContractModel

VERIFICATION_FAILED = "verification_failed"
NOT_FOUND = "not_found"
DEMO_VERIFICATION_CODE = "246810"
FIXTURE_ORDER_NUMBER = "NS-000123"
FIXTURE_POSTCODE = "SW1A 1AA"

_ASCII_UPPER = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class OrderRecord(ContractModel):
    """Allowlisted fictional order fields; never includes postcode or contact data."""

    order_number: StrictStr
    first_name: StrictStr
    item_names: list[StrictStr]
    status: Literal["active"]
    dispatch_date: StrictStr | None
    delivery_date: StrictStr | None


class OrderVerificationCommand(ContractModel):
    """Internal verification body; only the Order API sees postcode and code."""

    order_number: StrictStr | None
    delivery_postcode: Annotated[StrictStr, Field(max_length=64)]
    verification_code: Annotated[StrictStr, Field(max_length=64)]


class OrderVerificationResult(ContractModel):
    """Public verification outcome: one boolean and one indistinguishable error."""

    verified: StrictBool
    error: Literal["verification_failed"] | None

    @model_validator(mode="after")
    def _consistent(self) -> "OrderVerificationResult":
        if self.verified and self.error is not None:
            raise ValueError("a verified result cannot carry an error")
        if not self.verified and self.error != VERIFICATION_FAILED:
            raise ValueError("an unverified result must use verification_failed")
        return self


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


def credentials_match(order_number: str | None, delivery_postcode: str, verification_code: str) -> bool:
    """All three demo values must match; every mismatch is indistinguishable."""

    return (
        order_number == FIXTURE_ORDER_NUMBER
        and normalize_postcode(delivery_postcode) == FIXTURE_POSTCODE
        and valid_verification_code(verification_code)
        and verification_code == DEMO_VERIFICATION_CODE
    )


def create_order_api_app(
    orders: dict[str, OrderRecord] | None = None,
) -> FastAPI:
    """Build the private Order API; `orders` is a test seam for the no-match path."""

    catalog = fixture_orders() if orders is None else orders
    app = FastAPI(
        title="Northstar order API",
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_order_api_app()
