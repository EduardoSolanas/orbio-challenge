"""Run the Playwright journeys after the async suites.

pytest-playwright's session-scoped sync API runs an asyncio loop in a greenlet on
the main thread. When browser tests are collected first, pytest-asyncio's runner
setup for the unit/integration suites sees that loop as running and every async
test errors. Keeping the browser journeys last makes `pytest tests` a single
green run without changing any test.
"""

from __future__ import annotations

import functools
from typing import Any

import pytest


def pytest_collection_modifyitems(items: list[Any]) -> None:
    items.sort(key=lambda item: item.nodeid.startswith("tests/e2e/"))


@pytest.fixture(scope="session", autouse=True)
def share_one_tls_context() -> Any:
    """Build httpx's TLS context once per run instead of once per client.

    Every ``httpx`` client loads the CA bundle on construction, about 0.14 s each,
    and one test builds five of them. The tests talk to loopback over plain HTTP,
    so a single shared context changes nothing they check.
    """

    from httpx._transports import default

    patch = pytest.MonkeyPatch()
    patch.setattr(default, "create_ssl_context", functools.lru_cache(default.create_ssl_context))
    yield
    patch.undo()
