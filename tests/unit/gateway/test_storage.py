"""Unit tests for durable per-session JSON persistence (IC §3, §8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from gateway.errors import AppError
from gateway.storage import JsonSessionRepository
from tests.support.unit_fakes import make_message, make_session, make_slots

SESSION_ID = UUID("8b9dc0ee-1111-4a4a-8a8a-0123456789ab")


def repository(tmp_path):
    return JsonSessionRepository(tmp_path)


async def test_save_then_load_round_trips_the_full_session(tmp_path):
    repo = repository(tmp_path)
    session = make_session(
        session_id=SESSION_ID,
        messages=[make_message("I can help record an issue.")],
    )

    await repo.save(session)
    loaded = await repo.load(SESSION_ID)

    assert loaded == session
    assert loaded.session_id == SESSION_ID
    assert loaded.revision == 0
    assert loaded.status == "collecting"


async def test_save_writes_only_the_canonical_uuid_filename(tmp_path):
    repo = repository(tmp_path)
    session = make_session(session_id=SESSION_ID)

    await repo.save(session)

    files = sorted(path.name for path in tmp_path.iterdir())
    assert files == [f"{SESSION_ID}.json"]
    assert str(SESSION_ID) == str(SESSION_ID).lower()


async def test_saved_json_is_utf8_indented_and_does_not_escape_non_ascii(tmp_path):
    repo = repository(tmp_path)
    session = make_session(
        session_id=SESSION_ID,
        slots=make_slots(description="Café — naïve problem"),
    )

    await repo.save(session)

    raw = (tmp_path / f"{SESSION_ID}.json").read_bytes()
    text = raw.decode("utf-8")
    assert text.startswith('{\n  "schema_version": 2,')
    assert "Café — naïve problem" in text
    assert "\\u00e9" not in text
    assert json.loads(text)["session_id"] == str(SESSION_ID)


async def test_load_missing_session_raises_session_not_found(tmp_path):
    repo = repository(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await repo.load(uuid4())

    assert excinfo.value.code == "session_not_found"
    assert excinfo.value.status == 404
    assert excinfo.value.retryable is False


async def test_load_corrupt_json_raises_storage_corrupt(tmp_path):
    repo = repository(tmp_path)
    (tmp_path / f"{SESSION_ID}.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        await repo.load(SESSION_ID)

    assert excinfo.value.code == "storage_corrupt"
    assert excinfo.value.status == 503
    assert excinfo.value.retryable is False


async def test_load_future_schema_version_raises_storage_corrupt(tmp_path):
    repo = repository(tmp_path)
    payload = json.loads(
        make_session(session_id=SESSION_ID).model_dump_json()
    )
    payload["schema_version"] = 3
    (tmp_path / f"{SESSION_ID}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(AppError) as excinfo:
        await repo.load(SESSION_ID)

    assert excinfo.value.code == "storage_corrupt"


async def test_load_unknown_key_raises_storage_corrupt(tmp_path):
    repo = repository(tmp_path)
    payload = json.loads(
        make_session(session_id=SESSION_ID).model_dump_json()
    )
    payload["unexpected"] = True
    (tmp_path / f"{SESSION_ID}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(AppError) as excinfo:
        await repo.load(SESSION_ID)

    assert excinfo.value.code == "storage_corrupt"


async def test_check_writable_probes_with_a_real_temporary_file(tmp_path):
    repo = repository(tmp_path)

    await repo.check_writable()

    assert list(tmp_path.iterdir()) == []


async def test_save_failure_leaves_previous_bytes_and_no_replay_side_effect(
    tmp_path, monkeypatch
):
    import gateway.storage as storage_module

    repo = repository(tmp_path)
    first = make_session(session_id=SESSION_ID, revision=1)
    await repo.save(first)
    original = (tmp_path / f"{SESSION_ID}.json").read_bytes()

    second = make_session(session_id=SESSION_ID, revision=2)

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(AppError) as excinfo:
        await repo.save(second)

    assert excinfo.value.code == "storage_unavailable"
    assert excinfo.value.status == 503
    assert excinfo.value.retryable is True
    assert (tmp_path / f"{SESSION_ID}.json").read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        f"{SESSION_ID}.json"
    ]


async def test_save_stages_a_complete_replacement_in_the_session_directory(
    tmp_path, monkeypatch
):
    import gateway.storage as storage_module

    repo = repository(tmp_path)
    session = make_session(session_id=SESSION_ID)
    seen = {}
    real_replace = os.replace

    def recording_replace(src, dst):
        seen["src"] = src
        seen["dst"] = dst
        seen["staged"] = Path(src).read_bytes()
        real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", recording_replace)

    await repo.save(session)

    staged_path = Path(seen["src"])
    assert staged_path.parent == tmp_path
    assert staged_path.name != f"{SESSION_ID}.json"
    assert json.loads(seen["staged"]) == json.loads(
        (tmp_path / f"{SESSION_ID}.json").read_bytes()
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        f"{SESSION_ID}.json"
    ]


async def test_save_fsyncs_the_complete_replacement_before_publishing(
    tmp_path, monkeypatch
):
    import gateway.storage as storage_module

    repo = repository(tmp_path)
    session = make_session(session_id=SESSION_ID)
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(storage_module.os, "replace", recording_replace)

    await repo.save(session)

    assert events == ["fsync", "replace"]


async def test_check_writable_reports_storage_unavailable_when_probe_fails(
    tmp_path,
):
    repo = JsonSessionRepository(tmp_path / "missing")

    with pytest.raises(AppError) as excinfo:
        await repo.check_writable()

    assert excinfo.value.code == "storage_unavailable"
    assert excinfo.value.retryable is True


async def test_locks_serialize_concurrent_access_to_one_session(tmp_path):
    import asyncio

    repo = repository(tmp_path)
    entered = asyncio.Event()
    order = []

    async def holder():
        async with repo.locked(SESSION_ID):
            order.append("first-enter")
            await asyncio.sleep(0.05)
            order.append("first-exit")

    async def waiter():
        async with repo.locked(SESSION_ID):
            order.append("second-enter")

    first = asyncio.create_task(holder())
    await asyncio.sleep(0)
    second = asyncio.create_task(waiter())
    await asyncio.gather(first, second)

    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_lock_acquisition_timeout_is_two_seconds_and_session_busy(
    tmp_path,
):
    import asyncio
    import time

    repo = repository(tmp_path)
    started = time.monotonic()

    async with repo.locked(SESSION_ID):
        with pytest.raises(AppError) as excinfo:
            async with repo.locked(SESSION_ID):
                raise AssertionError("second lock must not be acquired")

    elapsed = time.monotonic() - started

    assert excinfo.value.code == "session_busy"
    assert excinfo.value.status == 429
    assert excinfo.value.retryable is True
    assert 1.9 <= elapsed < 3.0
    assert repo.locked(SESSION_ID) is not None


async def test_locks_are_independent_per_session(tmp_path):
    import asyncio

    repo = repository(tmp_path)
    other = uuid4()

    async with repo.locked(SESSION_ID):
        async with repo.locked(other):
            pass


async def test_load_io_error_maps_to_storage_unavailable(tmp_path):
    repo = repository(tmp_path)
    (tmp_path / f"{SESSION_ID}.json").mkdir()

    with pytest.raises(AppError) as excinfo:
        await repo.load(SESSION_ID)

    assert excinfo.value.code == "storage_unavailable"
    assert excinfo.value.retryable is True


async def test_save_into_missing_directory_is_storage_unavailable(tmp_path):
    repo = JsonSessionRepository(tmp_path / "missing")

    with pytest.raises(AppError) as excinfo:
        await repo.save(make_session(session_id=SESSION_ID))

    assert excinfo.value.code == "storage_unavailable"
    assert excinfo.value.retryable is True
