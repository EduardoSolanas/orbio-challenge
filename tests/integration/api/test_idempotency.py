"""Durable replay, request-ID conflicts, concurrency and cancellation.

Every case runs the real app factory, lifespan, routes, engine and JSON
repository over an isolated temp SESSION_DIR; only the AI boundary is scripted
(IC §13.1). Replay is proved against the persisted `requests`
records and the committed bytes on disk, never against an in-memory cache.

Concurrency cases hold one turn inside `extract` with an asyncio event, start
the competing request, and only then release the held turn — well before the
2-second session-lock deadline (IC §7). The scripted boundary selects its
result by the exact `current_message` so two concurrent turns cannot swap
scripted outcomes; it still never normalizes fields or chooses replies.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.main import create_app
from gateway.schemas import Extraction
from tests.integration.support.api import (
    ScriptedProvider,
    persisted_response,
    read_session,
    session_path,
)
from tests.integration.support.confirmation import (
    assert_error,
    receipt_for,
    review_session,
)
from tests.integration.support.intake import (
    CATEGORY_TEXT,
    FULL_SLOTS,
    FULL_SUMMARY,
    FULL_TEXT,
    PART_TEXT,
    full_extraction,
    part_extraction,
    start_session,
    turn_body,
)

REQUEST_ID_CONFLICT_TEXT = "This request ID was already used for different input."
REVISION_CONFLICT_TEXT = "This session changed. Refresh before sending again."
LOCK_WAIT_SECONDS = 2.0

PART_SLOTS = {
    "order_number": "NS-000123",
    "category": None,
    "description": "My order arrived broken",
    "urgency": None,
}


def turns_url(session_id: str) -> str:
    return f"/api/sessions/{session_id}/turns"


def confirm_url(session_id: str) -> str:
    return f"/api/sessions/{session_id}/confirm"


@asynccontextmanager
async def app_client(settings, provider):
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


class GatedProvider(ScriptedProvider):
    """Scripted AI boundary that holds each extraction on a per-message gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.results: dict[str, Extraction] = {}

    def script_for(self, message: str, extraction: Extraction) -> None:
        self.results[message] = extraction
        self.entered[message] = asyncio.Event()
        self.gates[message] = asyncio.Event()

    async def extract(self, context):
        self.calls.append(("extract", context))
        message = context.current_message
        self.entered[message].set()
        await self.gates[message].wait()
        return self.results[message]


async def wait_until_held(provider: GatedProvider, message: str) -> None:
    """Wait until the turn for `message` holds the session lock inside extract."""

    await asyncio.wait_for(provider.entered[message].wait(), timeout=LOCK_WAIT_SECONDS)


def assert_single_commit(disk: dict, *, revision: int, request_id: str, response: dict):
    assert disk["revision"] == revision
    assert disk["messages"][-2]["role"] == "user"
    assert disk["messages"][-1]["role"] == "assistant"
    assert disk["messages"][-1]["id"] == response["message_id"]
    assert len(disk["requests"]) == 1
    assert disk["requests"][0]["request_id"] == request_id
    assert disk["requests"][0]["response"] == persisted_response(response)


# resend identical committed turn request -------------------------------------------------------------------


async def case_resend_identical_committed_turn_request(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        request_id = str(uuid4())
        body = turn_body(PART_TEXT, 0, request_id)
        provider.script_extraction(part_extraction())

        first = await client.post(turns_url(session_id), json=body)
        assert first.status_code == 200, first.text
        calls_after_commit = len(provider.calls)

        # Identical network retry: same request ID and byte-identical body.
        second = await client.post(turns_url(session_id), json=body)
        assert second.status_code == 200, second.text
        assert second.text == first.text
        assert second.json() == first.json()
        assert len(provider.calls) == calls_after_commit  # E0: replay, no extraction

        disk = read_session(session_dir, session_id)
        assert_single_commit(
            disk, revision=1, request_id=request_id, response=first.json()
        )
        assert len(disk["messages"]) == 3
        assert disk["collection_turn_count"] == 1
        assert disk["slots"] == PART_SLOTS


# reuse request id different text body -------------------------------------------------------------------


async def case_reuse_request_id_different_text_body(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        request_id = str(uuid4())
        provider.script_extraction(part_extraction())
        committed = await client.post(
            turns_url(session_id), json=turn_body(PART_TEXT, 0, request_id)
        )
        assert committed.status_code == 200, committed.text

        target = session_path(session_dir, session_id)
        bytes_before = target.read_bytes()
        calls_before = len(provider.calls)

        # Same request ID, different text: always a digest conflict.
        conflict = await client.post(
            turns_url(session_id),
            json=turn_body("It is a damaged item.", 1, request_id),
        )
        await assert_error(
            conflict,
            status=409,
            code="request_id_conflict",
            message=REQUEST_ID_CONFLICT_TEXT,
        )
        assert target.read_bytes() == bytes_before
        assert len(provider.calls) == calls_before
        disk = read_session(session_dir, session_id)
        assert_single_commit(
            disk, revision=1, request_id=request_id, response=committed.json()
        )


# discard successful response after commit restart app replay -------------------------------------------------------------------


async def case_discard_successful_response_after_commit_restart_app_replay(
    settings, provider, session_dir
):
    request_id = str(uuid4())
    body = turn_body(PART_TEXT, 0, request_id)

    async with app_client(settings, provider) as first:
        session_id = await start_session(first)
        provider.script_extraction(part_extraction())
        original = await first.post(turns_url(session_id), json=body)
        assert original.status_code == 200, original.text
        original_text = original.text
        calls_after_commit = len(provider.calls)
        target = session_path(session_dir, session_id)
        committed_bytes = target.read_bytes()

    # The successful response was discarded; the app restarts over the same
    # directory and the client replays the exact request body/ID.
    async with app_client(settings, provider) as restarted:
        replay = await restarted.post(turns_url(session_id), json=body)
        assert replay.status_code == 200, replay.text
        assert replay.text == original_text
        assert len(provider.calls) == calls_after_commit  # no new extraction
        assert target.read_bytes() == committed_bytes  # no additional commit
        disk = read_session(session_dir, session_id)
        assert_single_commit(
            disk, revision=1, request_id=request_id, response=replay.json()
        )
        assert len(disk["messages"]) == 3


# concurrent same session revision distinct ids -------------------------------------------------------------------


async def case_concurrent_same_session_revision_distinct_ids(
    settings, provider, session_dir
):
    gated = GatedProvider()
    async with app_client(settings, gated) as client:
        session_id = await start_session(client)
        gated.script_for(PART_TEXT, part_extraction())
        first_body = turn_body(PART_TEXT, 0, str(uuid4()))
        second_body = turn_body(CATEGORY_TEXT, 0, str(uuid4()))

        first_task = asyncio.create_task(
            client.post(turns_url(session_id), json=first_body)
        )
        await wait_until_held(gated, PART_TEXT)
        second_task = asyncio.create_task(
            client.post(turns_url(session_id), json=second_body)
        )
        await asyncio.sleep(0.2)
        assert not second_task.done()  # genuinely in flight on the session lock

        gated.gates[PART_TEXT].set()  # release well before the 2 s lock deadline
        first = await first_task
        second = await second_task

        assert first.status_code == 200, first.text
        await assert_error(
            second,
            status=409,
            code="revision_conflict",
            message=REVISION_CONFLICT_TEXT,
            current_revision=1,
        )
        assert [name for name, _ in gated.calls] == ["extract"]
        disk = read_session(session_dir, session_id)
        assert_single_commit(
            disk, revision=1, request_id=first_body["request_id"], response=first.json()
        )
        assert len(disk["messages"]) == 3


# concurrent same exact id body -------------------------------------------------------------------


async def case_concurrent_same_exact_id_body(settings, provider, session_dir):
    gated = GatedProvider()
    async with app_client(settings, gated) as client:
        session_id = await start_session(client)
        gated.script_for(PART_TEXT, part_extraction())
        request_id = str(uuid4())
        body = turn_body(PART_TEXT, 0, request_id)

        first_task = asyncio.create_task(client.post(turns_url(session_id), json=body))
        await wait_until_held(gated, PART_TEXT)
        duplicate_task = asyncio.create_task(
            client.post(turns_url(session_id), json=body)
        )
        await asyncio.sleep(0.2)
        assert not duplicate_task.done()

        gated.gates[PART_TEXT].set()
        first = await first_task
        duplicate = await duplicate_task

        assert first.status_code == 200, first.text
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.text == first.text
        assert duplicate.json() == first.json()
        assert [name for name, _ in gated.calls] == ["extract"]  # one extraction
        disk = read_session(session_dir, session_id)
        assert_single_commit(
            disk, revision=1, request_id=request_id, response=first.json()
        )
        assert len(disk["messages"]) == 3


# concurrent independent session ids -------------------------------------------------------------------


async def case_concurrent_independent_session_ids(settings, provider, session_dir):
    gated = GatedProvider()
    async with app_client(settings, gated) as client:
        first_session = await start_session(client)
        second_session = await start_session(client)
        assert first_session != second_session

        gated.script_for(PART_TEXT, part_extraction())
        gated.script_for(FULL_TEXT, full_extraction())
        first_body = turn_body(PART_TEXT, 0, str(uuid4()))
        second_body = turn_body(FULL_TEXT, 0, str(uuid4()))

        first_task = asyncio.create_task(
            client.post(turns_url(first_session), json=first_body)
        )
        second_task = asyncio.create_task(
            client.post(turns_url(second_session), json=second_body)
        )
        # Both turns hold their own session lock at the same time.
        await asyncio.gather(
            wait_until_held(gated, PART_TEXT), wait_until_held(gated, FULL_TEXT)
        )
        gated.gates[PART_TEXT].set()
        gated.gates[FULL_TEXT].set()
        first, second = await asyncio.gather(first_task, second_task)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        first_disk = read_session(session_dir, first_session)
        assert first_disk["session_id"] == first_session
        assert first_disk["revision"] == 1
        assert first_disk["status"] == "collecting"
        assert first_disk["slots"] == PART_SLOTS
        assert first_disk["pending_review_revision"] is None
        assert first_disk["summary"] is None
        assert_single_commit(
            first_disk,
            revision=1,
            request_id=first_body["request_id"],
            response=first.json(),
        )
        assert first_disk["requests"][0]["response"]["session_id"] == first_session

        second_disk = read_session(session_dir, second_session)
        assert second_disk["session_id"] == second_session
        assert second_disk["revision"] == 1
        assert second_disk["status"] == "awaiting_confirmation"
        assert second_disk["slots"] == FULL_SLOTS
        assert second_disk["pending_review_revision"] == 1
        assert second_disk["summary"] == FULL_SUMMARY
        assert_single_commit(
            second_disk,
            revision=1,
            request_id=second_body["request_id"],
            response=second.json(),
        )
        assert second_disk["requests"][0]["response"]["session_id"] == second_session

        # No cross-session leakage in either direction.
        assert first_disk["slots"] != second_disk["slots"]
        assert first_disk["messages"][1]["text"] == PART_TEXT
        assert second_disk["messages"][1]["text"] == FULL_TEXT
        assert (
            first_disk["requests"][0]["request_id"]
            != second_disk["requests"][0]["request_id"]
        )


# same id but changed endpoint turn versus confirm -------------------------------------------------------------------


async def case_same_id_but_changed_endpoint_turn_versus_confirm(
    settings, provider, session_dir
):
    async with app_client(settings, provider) as client:
        # Turn committed first, then a confirm reusing that request ID.
        session_id = await review_session(client, provider)
        assert read_session(session_dir, session_id)["revision"] == 1
        confirm_id = str(uuid4())
        confirmed = await client.post(
            confirm_url(session_id),
            json={
                "request_id": confirm_id,
                "expected_revision": 1,
                "review_revision": 1,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "completed"
        assert confirmed.json()["reply"] == receipt_for(session_id)

        target = session_path(session_dir, session_id)
        bytes_before = target.read_bytes()
        calls_before = len(provider.calls)
        # Same ID through the turn endpoint: endpoint-aware digest mismatch.
        turn_conflict = await client.post(
            turns_url(session_id), json=turn_body("Yes.", 1, confirm_id)
        )
        await assert_error(
            turn_conflict,
            status=409,
            code="request_id_conflict",
            message=REQUEST_ID_CONFLICT_TEXT,
        )
        assert target.read_bytes() == bytes_before
        assert len(provider.calls) == calls_before  # dedup precedes extraction

        # The reverse direction: turn committed, then confirm with that ID.
        other_session = await start_session(client)
        turn_id = str(uuid4())
        provider.script_extraction(part_extraction())
        turned = await client.post(
            turns_url(other_session), json=turn_body(PART_TEXT, 0, turn_id)
        )
        assert turned.status_code == 200, turned.text
        other_target = session_path(session_dir, other_session)
        other_bytes = other_target.read_bytes()
        confirm_conflict = await client.post(
            confirm_url(other_session),
            json={
                "request_id": turn_id,
                "expected_revision": 1,
                "review_revision": 0,
            },
        )
        await assert_error(
            confirm_conflict,
            status=409,
            code="request_id_conflict",
            message=REQUEST_ID_CONFLICT_TEXT,
        )
        assert other_target.read_bytes() == other_bytes
        other_disk = read_session(session_dir, other_session)
        assert other_disk["revision"] == 1
        assert len(other_disk["requests"]) == 1
        assert other_disk["status"] == "collecting"


# cancel request before atomic save -------------------------------------------------------------------


async def case_cancel_request_before_atomic_save(settings, provider, session_dir):
    gated = GatedProvider()
    async with app_client(settings, gated) as client:
        session_id = await start_session(client)
        target = session_path(session_dir, session_id)
        original_bytes = target.read_bytes()
        gated.script_for(PART_TEXT, part_extraction())
        request_id = str(uuid4())
        body = turn_body(PART_TEXT, 0, request_id)

        task = asyncio.create_task(client.post(turns_url(session_id), json=body))
        await wait_until_held(gated, PART_TEXT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancelled before the atomic save: no pair, no replay record, no
        # revision change and no leftover temp file.
        assert target.read_bytes() == original_bytes
        disk = read_session(session_dir, session_id)
        assert disk["revision"] == 0
        assert len(disk["messages"]) == 1
        assert disk["requests"] == []
        assert disk["collection_turn_count"] == 0
        assert [p.name for p in session_dir.iterdir()] == [target.name]

        # The lock was released: a later retry commits exactly once.
        gated.script_for(PART_TEXT, part_extraction())
        retry = asyncio.create_task(client.post(turns_url(session_id), json=body))
        await wait_until_held(gated, PART_TEXT)
        gated.gates[PART_TEXT].set()
        response = await retry
        assert response.status_code == 200, response.text
        disk = read_session(session_dir, session_id)
        assert_single_commit(
            disk, revision=1, request_id=request_id, response=response.json()
        )
        assert len(disk["messages"]) == 3
        assert disk["collection_turn_count"] == 1


CASES = [
    (
        "resend_identical_committed_turn_request",
        case_resend_identical_committed_turn_request,
    ),
    ("reuse_request_id_different_text_body", case_reuse_request_id_different_text_body),
    (
        "discard_successful_response_after_commit_restart_app_replay",
        case_discard_successful_response_after_commit_restart_app_replay,
    ),
    (
        "concurrent_same_session_revision_distinct_ids",
        case_concurrent_same_session_revision_distinct_ids,
    ),
    ("concurrent_same_exact_id_body", case_concurrent_same_exact_id_body),
    ("concurrent_independent_session_ids", case_concurrent_independent_session_ids),
    (
        "same_id_but_changed_endpoint_turn_versus_confirm",
        case_same_id_but_changed_endpoint_turn_versus_confirm,
    ),
    ("cancel_request_before_atomic_save", case_cancel_request_before_atomic_save),
]


@pytest.mark.parametrize("case_id,case", CASES, ids=[case_id for case_id, _ in CASES])
async def test_idempotency_and_concurrency(
    case_id, case, settings, provider, session_dir
):
    await case(settings, provider, session_dir)
