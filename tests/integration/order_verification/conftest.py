"""Fixtures for the order verification tests."""

from __future__ import annotations

import pytest

from backend.gateway.main import create_app
from backend.gateway.backend_client import BackendApiClient
from backend.gateway.conversation.verification import VerificationRegistry
from tests.integration.order_verification.support import FakeClock
from tests.integration.support.api import running_client
from tests.integration.support.backend_api import LocalBackendApiServer


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def verification(clock: FakeClock) -> VerificationRegistry:
    return VerificationRegistry(clock=clock)


@pytest.fixture
def backend_api() -> LocalBackendApiServer:
    with LocalBackendApiServer() as server:
        yield server


@pytest.fixture
def app(settings, provider, verification, backend_api):
    return create_app(
        settings=settings,
        provider=provider,
        verification_registry=verification,
        backend_api_client=BackendApiClient(backend_api.base_url),
    )


@pytest.fixture
async def client(app):
    async with running_client(app) as active_client:
        yield active_client
