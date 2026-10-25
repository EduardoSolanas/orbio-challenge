"""The wait helpers: quick when ready, backing off when not, always bounded."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.support.waiting import apoll, await_until, poll, wait_until


def test_a_true_condition_is_checked_once() -> None:
    calls = []

    assert poll(lambda: calls.append(1) or True, timeout=1)
    assert len(calls) == 1


def test_a_condition_that_turns_true_is_noticed() -> None:
    calls = []

    def third_time() -> bool:
        calls.append(time.monotonic())
        return len(calls) == 3

    assert poll(third_time, timeout=2)
    assert len(calls) == 3


def test_the_pauses_between_checks_grow() -> None:
    stamps = []

    def never() -> bool:
        stamps.append(time.monotonic())
        return False

    assert not poll(never, timeout=0.3, longest=0.1)
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
    assert gaps[-1] > gaps[0]
    assert max(gaps) < 0.25


def test_a_condition_that_never_holds_gives_up_at_the_deadline() -> None:
    started = time.monotonic()

    assert poll(lambda: False, timeout=0.2) is False
    assert 0.15 <= time.monotonic() - started < 1.0


def test_an_exception_in_the_condition_is_not_swallowed() -> None:
    def broken() -> bool:
        raise OSError("boom")

    with pytest.raises(OSError, match="boom"):
        poll(broken, timeout=1)


def test_wait_until_names_what_never_happened() -> None:
    with pytest.raises(AssertionError, match="waiting for the lock to clear"):
        wait_until(lambda: False, "the lock to clear", timeout=0.1)


async def test_the_async_wait_lets_other_tasks_run() -> None:
    flag = []

    async def set_later() -> None:
        await asyncio.sleep(0.05)
        flag.append(1)

    task = asyncio.create_task(set_later())
    await await_until(lambda: bool(flag), "the other task", timeout=2)
    await task
    assert await apoll(lambda: False, timeout=0.1) is False
