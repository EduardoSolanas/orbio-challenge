"""The gateway's Backend API client against the real Backend API."""

from __future__ import annotations

from backend_api.main import FIXTURE_ORDER_NUMBER
from gateway.backend_client import BackendApiClient


async def test_an_unknown_accounts_installation_is_nothing_and_a_known_one_is_read(
    backend_api,
):
    client = BackendApiClient(backend_api.base_url)
    try:
        assert await client.installation("NS-999999") is None
        record = await client.installation(FIXTURE_ORDER_NUMBER)
    finally:
        await client.aclose()

    assert record is not None and record.appointment is None
