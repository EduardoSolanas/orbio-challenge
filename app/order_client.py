"""Gateway client for the private Order API boundary.

The gateway has no catalog and no demo code: every verification and lookup
crosses a real HTTP boundary to the private service. Transport failures map to
fixed safe application errors so a browser never receives transport detail.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.errors import (
    INVALID_ORDER_API_RESPONSE,
    ORDER_API_TIMEOUT,
    ORDER_API_UNAVAILABLE,
    app_error,
)
from app.order_api import OrderRecord, OrderVerificationResult

VERIFICATIONS_PATH = "/internal/v1/order-verifications"
ORDERS_PATH = "/internal/v1/orders"


class OrderApiClient:
    """Async HTTP client for the private Order API; one instance per gateway."""

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
        """Ask the Order API whether these demo credentials match the order."""

        payload = {
            "order_number": order_number,
            "delivery_postcode": delivery_postcode,
            "verification_code": verification_code,
        }
        response = await self._request("POST", VERIFICATIONS_PATH, json=payload)
        return self._validated(OrderVerificationResult, response)

    async def lookup_order(self, order_number: str) -> OrderRecord | None:
        """Return the allowlisted record, or None when the Order API has no match."""

        response = await self._request(
            "GET", f"{ORDERS_PATH}/{quote(order_number, safe='')}", allow_not_found=True
        )
        if response.status_code == 404:
            return None
        return self._validated(OrderRecord, response)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, allow_not_found: bool = False, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.ConnectTimeout:
            raise app_error(ORDER_API_UNAVAILABLE) from None
        except httpx.ConnectError:
            raise app_error(ORDER_API_UNAVAILABLE) from None
        except httpx.TimeoutException:
            raise app_error(ORDER_API_TIMEOUT) from None
        except httpx.HTTPError:
            raise app_error(INVALID_ORDER_API_RESPONSE) from None
        if response.status_code in (502, 503):
            raise app_error(ORDER_API_UNAVAILABLE)
        if response.status_code == 504:
            raise app_error(ORDER_API_TIMEOUT)
        if response.status_code == 404 and allow_not_found:
            return response
        if response.status_code != 200:
            raise app_error(INVALID_ORDER_API_RESPONSE)
        return response

    @staticmethod
    def _validated(model, response: httpx.Response):
        try:
            return model.model_validate(response.json())
        except (ValidationError, ValueError):
            raise app_error(INVALID_ORDER_API_RESPONSE) from None
