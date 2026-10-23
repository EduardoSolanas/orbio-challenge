"""Run the Playwright journeys after the async suites.

pytest-playwright's session-scoped sync API runs an asyncio loop in a greenlet on
the main thread. When browser tests are collected first, pytest-asyncio's runner
setup for the unit/integration suites sees that loop as running and every async
test errors. Keeping the browser journeys last makes `pytest tests` a single
green run without changing any test.
"""

from __future__ import annotations

from typing import Any


def pytest_collection_modifyitems(items: list[Any]) -> None:
    items.sort(key=lambda item: item.nodeid.startswith("tests/e2e/"))
