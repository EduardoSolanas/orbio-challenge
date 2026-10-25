"""Gateway client for the private Backend API boundary.

The gateway has no catalog and no demo code: every verification and lookup
crosses a real HTTP boundary to the private service. Transport failures map to
fixed safe application errors so a browser never receives transport detail.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.backend_api import (
    Appointment,
    Availability,
    InstallationRecord,
    OrderRecord,
    OrderVerificationResult,
)
from app.bookings import ALREADY_BOOKED, SLOT_UNAVAILABLE
from app.errors import (
    BACKEND_API_TIMEOUT,
    BACKEND_API_UNAVAILABLE,
    INVALID_BACKEND_API_RESPONSE,
    app_error,
)

VERIFICATIONS_PATH = "/internal/v1/order-verifications"
ORDERS_PATH = "/internal/v1/orders"
AVAILABILITY_PATH = "/internal/v1/availability"
BOOKINGS_PATH = "/internal/v1/bookings"


class BackendApiClient:
    """Async HTTP client for the private Backend API; one instance per gateway."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def verify_order(
        self, order_number: str | None, delivery_postcode: str, verification_code: str
    ) -> OrderVerificationResult:
        """Ask the Backend API whether these demo credentials match the order."""

        payload = {
            "order_number": order_number,
            "delivery_postcode": delivery_postcode,
            "verification_code": verification_code,
        }
        response = await self._request("POST", VERIFICATIONS_PATH, json=payload)
        return self._validated(OrderVerificationResult, response)

    async def lookup_order(self, order_number: str) -> OrderRecord | None:
        """Return the allowlisted record, or None when the Backend API has no match."""

        response = await self._request(
            "GET", f"{ORDERS_PATH}/{quote(order_number, safe='')}", allow_not_found=True
        )
        if response.status_code == 404:
            return None
        return self._validated(OrderRecord, response)

    async def installation(self, order_number: str) -> InstallationRecord | None:
        """Installation progress for the account, or None when it is unknown."""

        response = await self._request(
            "GET",
            f"{ORDERS_PATH}/{quote(order_number, safe='')}/installation",
            allow_not_found=True,
        )
        if response.status_code == 404:
            return None
        return self._validated(InstallationRecord, response)

    async def availability(self) -> Availability:
        """The next free technician windows."""

        return self._validated(Availability, await self._request("GET", AVAILABILITY_PATH))

    async def book(self, order_number: str, starts_at: str) -> Appointment:
        """Reserve one window; a refused booking raises its fixed application error."""

        response = await self._request(
            "POST",
            BOOKINGS_PATH,
            json={"order_number": order_number, "starts_at": starts_at},
            allow_conflict=True,
        )
        if response.status_code == 409:
            try:
                code = response.json()["error"]
            except (ValueError, KeyError, TypeError):
                code = None
            raise app_error(
                code if code in (SLOT_UNAVAILABLE, ALREADY_BOOKED) else INVALID_BACKEND_API_RESPONSE
            )
        return self._validated(Appointment, response)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
        allow_conflict: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.ConnectTimeout:
            raise app_error(BACKEND_API_UNAVAILABLE) from None
        except httpx.ConnectError:
            raise app_error(BACKEND_API_UNAVAILABLE) from None
        except httpx.TimeoutException:
            raise app_error(BACKEND_API_TIMEOUT) from None
        except httpx.HTTPError:
            raise app_error(INVALID_BACKEND_API_RESPONSE) from None
        if response.status_code in (502, 503):
            raise app_error(BACKEND_API_UNAVAILABLE)
        if response.status_code == 504:
            raise app_error(BACKEND_API_TIMEOUT)
        if response.status_code == 404 and allow_not_found:
            return response
        if response.status_code == 409 and allow_conflict:
            return response
        if response.status_code != 200:
            raise app_error(INVALID_BACKEND_API_RESPONSE)
        return response

    @staticmethod
    def _validated(model, response: httpx.Response):
        try:
            return model.model_validate(response.json())
        except (ValidationError, ValueError):
            raise app_error(INVALID_BACKEND_API_RESPONSE) from None
