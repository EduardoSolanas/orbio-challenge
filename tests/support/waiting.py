"""Bounded waits for the test suite: poll with exponential backoff, never a bare sleep.

A condition that is already true costs one call. One that is not yet true is
retried after 10 ms, then 20 ms, 40 ms and so on up to ``longest`` seconds, so a
fast server is noticed quickly and a slow one is not hammered. Every wait has a
deadline; ``wait_until`` fails with a message naming what never happened.
"""

from __future__ import annotations

from typing import Callable

from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_result,
    stop_after_delay,
    wait_exponential,
)

FIRST_WAIT = 0.01
LONGEST_WAIT = 0.25
DEFAULT_TIMEOUT = 10.0


def _policy(timeout: float, longest: float) -> dict:
    return {
        "stop": stop_after_delay(timeout),
        "wait": wait_exponential(multiplier=FIRST_WAIT, max=longest),
        "retry": retry_if_result(lambda done: not done),
        # Out of time: report the last answer (false) instead of raising RetryError.
        "retry_error_callback": lambda state: False,
    }


def poll(
    condition: Callable[[], bool],
    timeout: float = DEFAULT_TIMEOUT,
    longest: float = LONGEST_WAIT,
) -> bool:
    """True as soon as ``condition()`` is true, False if ``timeout`` runs out first.

    An exception raised by ``condition`` propagates at once; it is not retried.
    """

    return bool(Retrying(**_policy(timeout, longest))(condition))


async def apoll(
    condition: Callable[[], bool],
    timeout: float = DEFAULT_TIMEOUT,
    longest: float = LONGEST_WAIT,
) -> bool:
    """``poll`` for async tests: waits with ``asyncio.sleep`` so the loop keeps running."""

    async def check() -> bool:
        return condition()

    return bool(await AsyncRetrying(**_policy(timeout, longest))(check))


def wait_until(
    condition: Callable[[], bool],
    what: str,
    timeout: float = DEFAULT_TIMEOUT,
    longest: float = LONGEST_WAIT,
) -> None:
    if not poll(condition, timeout, longest):
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")


async def await_until(
    condition: Callable[[], bool],
    what: str,
    timeout: float = DEFAULT_TIMEOUT,
    longest: float = LONGEST_WAIT,
) -> None:
    if not await apoll(condition, timeout, longest):
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")
