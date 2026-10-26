"""Asking the Backend API to move a visit sends the flag; an ordinary booking does not."""

from __future__ import annotations

import json

import httpx

from gateway.backend_client import BackendApiClient

APPOINTMENT = {
    "starts_at": "2026-11-02T11:00",
    "ends_at": "2026-11-02T13:00",
    "reference": "BK-0D8B3C88",
}


async def body_sent(**options) -> dict:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=APPOINTMENT)

    client = BackendApiClient(
        "http://backend.test", transport=httpx.MockTransport(handler)
    )
    try:
        await client.book("NS-000123", "2026-11-02T11:00", **options)
    finally:
        await client.aclose()
    return json.loads(seen[0].content)


async def test_an_ordinary_booking_sends_no_replace_flag() -> None:
    assert await body_sent() == {
        "order_number": "NS-000123",
        "starts_at": "2026-11-02T11:00",
    }


async def test_moving_a_visit_sends_the_replace_flag() -> None:
    assert await body_sent(replace=True) == {
        "order_number": "NS-000123",
        "starts_at": "2026-11-02T11:00",
        "replace": True,
    }
