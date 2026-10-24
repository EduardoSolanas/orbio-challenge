"""Fixtures for the order verification tests."""

from __future__ import annotations

import pytest

from app.main import create_app
from app.order_client import OrderApiClient
from app.verification import VerificationRegistry
from tests.integration.order_verification.support import FakeClock
from tests.integration.support_api import running_client
from tests.integration.support_order_api import LocalOrderApiServer


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def verification(clock: FakeClock) -> VerificationRegistry:
    return VerificationRegistry(clock=clock)


@pytest.fixture
def order_api() -> LocalOrderApiServer:
    with LocalOrderApiServer() as server:
        yield server


@pytest.fixture
def app(settings, provider, verification, order_api):
    return create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        order_api_client=OrderApiClient(order_api.base_url),
    )


@pytest.fixture
async def client(app):
    async with running_client(app) as active_client:
        yield active_client
