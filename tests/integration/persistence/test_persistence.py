"""Durability, atomic-save failure and restart resume.

Every case runs the real app factory, lifespan, routes, engine and JSON
repository against one isolated temp SESSION_DIR; only the AI service boundary
is scripted (IC §13.1). Restart cases stop the first app (lifespan
exit) and build a second real instance over the same directory.

Failure seams stay test-scoped and portable: `os.replace` is wrapped so it
fails only for the target document and delegates every other call to the real
implementation (no chmod tricks). Corrupt and future-schema documents are
fixture bytes written into the temp directory; the repository itself is never
replaced by a fake store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

from gateway.providers.prompts import GREETING, INVALID_URGENCY, MISSING_URGENCY
from gateway.schemas import Session
from tests.integration.support.api import (
    make_extraction,
    persisted_response,
    raw_asgi,
    read_session,
    served_messages,
    session_files,
    session_path,
)
from tests.integration.support.confirmation import (
    CHANGE_TEXT,
    LOW_SUMMARY,
    assert_session_state,
    change_extraction,
    rejected_session,
)
from tests.integration.support.intake import (
    CATEGORY_TEXT,
    FULL_SLOTS,
    PART_TEXT,
    REVIEW_SUFFIX,
    category_extraction,
    part_extraction,
    post_turn,
    start_session,
    turn_body,
)
from tests.integration.support.persistence import app_client, app_running

STORAGE_UNAVAILABLE_TEXT = "Session storage is unavailable. Please retry."
STORAGE_CORRUPT_TEXT = "This session cannot be loaded. Contact the demo operator."
INVALID_REQUEST_TEXT = "The request does not match the expected format."

PART_SLOTS = {
    "order_number": "NS-000123",
    "category": None,
    "description": "My order arrived broken",
    "urgency": None,
}



def assert_error_envelope(response, *, status, code, message, retryable):
    """Exact error envelope; storage errors keep their table retryability."""

    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    assert set(body) == {"error", "request_id"}
    return body


def assert_no_temp_files(session_dir: Path) -> None:
    """Only complete `<uuid>.json` documents remain; every temp file is gone."""

    leftovers = [p.name for p in session_dir.iterdir() if not p.name.endswith(".json")]
    assert leftovers == []


# commit normal partial turn reopen file -------------------------------------------------------------------


async def case_commit_normal_partial_turn_reopen_file(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        request_id, response = await post_turn(
            client,
            provider,
            session_id,
            text=PART_TEXT,
            revision=0,
            extraction=part_extraction(),
        )

        files = session_files(session_dir)
        assert files == [session_path(session_dir, session_id)]
        raw = files[0].read_bytes()
        assert_no_temp_files(session_dir)

        # The whole document decodes as one valid Session: no half-written JSON.
        document = json.loads(raw.decode("utf-8"))
        session = Session.model_validate_json(raw)
        assert session.schema_version == 2
        assert raw == json.dumps(
            session.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode("utf-8")

        assert document["session_id"] == session_id
        assert document["revision"] == 1
        assert document["collection_turn_count"] == 1
        assert document["status"] == "collecting"
        assert document["slots"] == PART_SLOTS
        assert document["pending_clarifications"] == {}
        assert document["pending_review_revision"] is None
        assert document["summary"] is None
        assert document["review_rejected"] is False
        assert document["confirmation"] is None

        assert [message["role"] for message in document["messages"]] == [
            "assistant",
            "user",
            "assistant",
        ]
        greeting, user, assistant = document["messages"]
        assert greeting["text"] == GREETING
        assert greeting["input_mode"] == "system"
        assert user["text"] == PART_TEXT
        assert user["input_mode"] == "text"
        assert user["citations"] == []
        assert assistant["id"] == response["message_id"]
        assert assistant["text"] == response["reply"]
        assert assistant["input_mode"] == "system"
        assert assistant["citations"] == []

        assert len(document["requests"]) == 1
        record = document["requests"][0]
        assert record["request_id"] == request_id
        assert len(record["input_digest"]) == 64
        assert record["input_digest"] == record["input_digest"].lower()
        assert record["response"] == persisted_response(response)


# stop app new instance same directory resume partial -------------------------------------------------------------------


async def case_stop_app_new_instance_same_directory_resume_partial(settings, provider, session_dir):
    async with app_client(settings, provider) as first:
        session_id = await start_session(first)
        _, response = await post_turn(
            first,
            provider,
            session_id,
            text=PART_TEXT,
            revision=0,
            extraction=part_extraction(),
        )
        assert response["slots"] == PART_SLOTS
        committed = read_session(session_dir, session_id)
        calls_after_turn = len(provider.calls)

    # Stop the app (lifespan exit) and resume through a brand new instance that
    # shares only the directory on disk.
    async with app_client(settings, provider) as second:
        resumed = await second.get(f"/api/sessions/{session_id}")
        assert resumed.status_code == 200, resumed.text
        view = resumed.json()
        assert view["session_id"] == session_id
        assert view["revision"] == committed["revision"] == 1
        assert view["status"] == "collecting"
        assert view["slots"] == PART_SLOTS
        assert served_messages(view["messages"]) == committed["messages"]
        assert view["pending_clarifications"] == {}
        assert view["pending_review_revision"] is None
        assert view["summary"] is None
        assert len(provider.calls) == calls_after_turn  # GET never calls a model

        _, continued = await post_turn(
            second,
            provider,
            session_id,
            text=CATEGORY_TEXT,
            revision=1,
            extraction=category_extraction(),
        )
        assert continued["slots"] == {**PART_SLOTS, "category": "damaged_item"}
        assert continued["reply"] == MISSING_URGENCY
        assert continued["revision"] == 2
        assert continued["status"] == "collecting"
        assert continued["pending_review_revision"] is None
        disk = read_session(session_dir, session_id)
        assert disk["messages"][:3] == served_messages(view["messages"])
        assert disk["revision"] == 2
        assert len(disk["requests"]) == 2
        assert disk["requests"][-1]["response"] == persisted_response(continued)


# scoped os replace failure during turn save -------------------------------------------------------------------


def scoped_replace_failure(target: Path, real_replace):
    """`os.replace` that fails only for `target`; everything else is real."""

    def replace(src, dst, *args, **kwargs):
        if Path(dst) == target:
            raise OSError("scoped os.replace failure")
        return real_replace(src, dst, *args, **kwargs)

    return replace


async def case_scoped_os_replace_failure_during_turn_save(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        _, response = await post_turn(
            client,
            provider,
            session_id,
            text=PART_TEXT,
            revision=0,
            extraction=part_extraction(),
        )
        assert response["revision"] == 1
        target = session_path(session_dir, session_id)
        original_bytes = target.read_bytes()
        original_disk = read_session(session_dir, session_id)
        calls_before = len(provider.calls)

        request_id = str(uuid4())
        body = turn_body(CATEGORY_TEXT, 1, request_id)
        provider.script_extraction(category_extraction())
        real_replace = os.replace
        with mock.patch(
            "gateway.storage.os.replace",
            side_effect=scoped_replace_failure(target, real_replace),
        ):
            failed = await client.post(
                f"/api/sessions/{session_id}/turns", json=body
            )

        assert_error_envelope(
            failed,
            status=503,
            code="storage_unavailable",
            message=STORAGE_UNAVAILABLE_TEXT,
            retryable=True,
        )
        assert target.read_bytes() == original_bytes
        assert session_files(session_dir) == [target]
        assert_no_temp_files(session_dir)
        after = read_session(session_dir, session_id)
        assert after == original_disk
        assert len(provider.calls) == calls_before + 1  # extraction happened, save failed

        # No phantom replay record: the same request is not a replay and can
        # commit exactly once after storage recovers.
        provider.script_extraction(category_extraction())
        retry = await client.post(f"/api/sessions/{session_id}/turns", json=body)
        assert retry.status_code == 200, retry.text
        payload = retry.json()
        assert payload["revision"] == 2
        assert payload["slots"] == {**PART_SLOTS, "category": "damaged_item"}
        disk = read_session(session_dir, session_id)
        assert disk["revision"] == 2
        assert len(disk["messages"]) == len(original_disk["messages"]) + 2
        assert len(disk["requests"]) == 2
        assert disk["requests"][-1]["request_id"] == request_id
        assert disk["requests"][-1]["response"] == {key: value for key, value in payload.items() if key != "speech_available"}
        assert_no_temp_files(session_dir)


# scoped create write replace failure -------------------------------------------------------------------


async def case_scoped_create_write_replace_failure(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        def failing_create(*args, **kwargs):
            raise OSError("scoped temp-file create failure")

        # Failed new-file create: no document, no false 201, nothing staged.
        with mock.patch(
            "gateway.storage.tempfile.NamedTemporaryFile", side_effect=failing_create
        ):
            created = await client.post("/api/sessions", json={})

        assert_error_envelope(
            created,
            status=503,
            code="storage_unavailable",
            message=STORAGE_UNAVAILABLE_TEXT,
            retryable=True,
        )
        assert session_files(session_dir) == []
        assert list(session_dir.iterdir()) == []

        def replace(src, dst, *args, **kwargs):
            raise OSError("scoped os.replace failure")

        # Failed replace after a complete staged write: temp cleaned, no file.
        with mock.patch("gateway.storage.os.replace", side_effect=replace):
            created = await client.post("/api/sessions", json={})

        assert_error_envelope(
            created,
            status=503,
            code="storage_unavailable",
            message=STORAGE_UNAVAILABLE_TEXT,
            retryable=True,
        )
        assert session_files(session_dir) == []
        assert list(session_dir.iterdir()) == []  # temp file cleaned, no false 201

        # Storage recovers: a real create publishes exactly one document.
        created = await client.post("/api/sessions", json={})
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        assert session_files(session_dir) == [session_path(session_dir, session_id)]
        assert_no_temp_files(session_dir)


# corrupt existing session json -------------------------------------------------------------------


async def case_corrupt_existing_session_json(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        target = session_path(session_dir, session_id)
        corrupt = b'{"schema_version": 1, "session_id": "' + session_id.encode(
            "ascii"
        ) + b'", "revision": '
        target.write_bytes(corrupt)

        loaded = await client.get(f"/api/sessions/{session_id}")
        assert_error_envelope(
            loaded,
            status=503,
            code="storage_corrupt",
            message=STORAGE_CORRUPT_TEXT,
            retryable=False,
        )
        assert target.read_bytes() == corrupt

        provider.script_extraction(part_extraction())
        turned = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body(PART_TEXT, 0),
        )
        assert_error_envelope(
            turned,
            status=503,
            code="storage_corrupt",
            message=STORAGE_CORRUPT_TEXT,
            retryable=False,
        )
        # No automatic repair, no overwrite, no phantom commit.
        assert target.read_bytes() == corrupt
        assert session_files(session_dir) == [target]
        assert_no_temp_files(session_dir)
        assert provider.calls == []


# persisted future schema version -------------------------------------------------------------------


async def case_persisted_future_schema_version(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await start_session(client)
        target = session_path(session_dir, session_id)
        document = json.loads(target.read_bytes().decode("utf-8"))
        document["schema_version"] = 3
        future = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        target.write_bytes(future)

        loaded = await client.get(f"/api/sessions/{session_id}")
        assert_error_envelope(
            loaded,
            status=503,
            code="storage_corrupt",
            message=STORAGE_CORRUPT_TEXT,
            retryable=False,
        )
        assert target.read_bytes() == future

        turned = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body(PART_TEXT, 0),
        )
        assert_error_envelope(
            turned,
            status=503,
            code="storage_corrupt",
            message=STORAGE_CORRUPT_TEXT,
            retryable=False,
        )
        # No downgrade and no overwrite of the future-version document.
        assert target.read_bytes() == future
        assert json.loads(future.decode("utf-8"))["schema_version"] == 3
        assert session_files(session_dir) == [target]
        assert_no_temp_files(session_dir)
        assert provider.calls == []


# path like session id through public route -------------------------------------------------------------------


async def case_path_like_session_id_through_public_route(settings, provider, session_dir):
    outside = session_dir.parent / "evil.json"
    traversal = "..%2F..%2Fsessions%2Fevil.json"

    async with app_running(settings, provider) as app:
        status, _, body = await raw_asgi(
            app,
            method="GET",
            path=f"/api/sessions/{traversal}",
            chunks=[b""],
        )
        assert status == 422
        payload = json.loads(body.decode("utf-8"))
        assert payload["error"] == {
            "code": "invalid_request",
            "message": INVALID_REQUEST_TEXT,
            "retryable": False,
        }

        status, _, body = await raw_asgi(
            app,
            method="POST",
            path=f"/api/sessions/{traversal}/turns",
            chunks=[
                json.dumps(turn_body(PART_TEXT, 0)).encode("utf-8"),
            ],
            headers={"content-type": "application/json"},
        )
        assert status == 422
        payload = json.loads(body.decode("utf-8"))
        assert payload["error"]["code"] == "invalid_request"

        # Nothing outside the configured directory was read or written.
        assert not outside.exists()
        assert session_files(session_dir) == []
        assert list(session_dir.iterdir()) == []
        assert provider.calls == []


def make_invalid_urgency_extraction():
    return make_extraction(
        intent="correction",
        candidates={"urgency": "sometime"},
        explicit_corrections=["urgency"],
    )


# resume rejected session pending issue -------------------------------------------------------------------


async def case_resume_rejected_session_pending_issue(settings, provider, session_dir):
    async with app_client(settings, provider) as client:
        session_id = await rejected_session(client, provider)
        rejected_disk = read_session(session_dir, session_id)
        assert rejected_disk["review_rejected"] is True
        assert rejected_disk["pending_review_revision"] is None

        # An invalid urgency candidate keeps the rejection and records the issue.
        provider.script_extraction(
            make_invalid_urgency_extraction()
        )
        request_id = str(uuid4())
        response = await client.post(
            f"/api/sessions/{session_id}/turns",
            json=turn_body("Sometime soon.", 2, request_id),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert_session_state(
            payload,
            revision=3,
            status="collecting",
            slots=FULL_SLOTS,
            summary=None,
            token=None,
            pending={
                "urgency": {
                    "code": "invalid",
                    "candidate": "sometime",
                    "question": INVALID_URGENCY,
                }
            },
        )
        assert payload["reply"] == INVALID_URGENCY
        disk = read_session(session_dir, session_id)
        assert disk["review_rejected"] is True
        assert disk["pending_review_revision"] is None
        assert disk["pending_clarifications"] == payload["pending_clarifications"]

        # Resume across a real restart: flag, issues and null token survive.
        calls_after_turn = len(provider.calls)
        async with app_client(settings, provider) as restarted:
            resumed = await restarted.get(f"/api/sessions/{session_id}")
            assert resumed.status_code == 200, resumed.text
            view = resumed.json()
            assert_session_state(
                view,
                revision=3,
                status="collecting",
                slots=FULL_SLOTS,
                summary=None,
                token=None,
                pending=payload["pending_clarifications"],
            )
            assert len(provider.calls) == calls_after_turn
            assert read_session(session_dir, session_id)["review_rejected"] is True

            # Explicit recovery follows section 5: one accepted correction
            # clears the issue and re-emits a fresh review without completing.
            provider.script_extraction(change_extraction())
            recovered = await restarted.post(
                f"/api/sessions/{session_id}/turns",
                json=turn_body(CHANGE_TEXT, 3),
            )
            assert recovered.status_code == 200, recovered.text
            recovered_payload = recovered.json()
            assert_session_state(
                recovered_payload,
                revision=4,
                status="awaiting_confirmation",
                slots={**FULL_SLOTS, "urgency": "low"},
                summary=LOW_SUMMARY,
                token=4,
            )
            assert recovered_payload["reply"] == LOW_SUMMARY + REVIEW_SUFFIX
            final_disk = read_session(session_dir, session_id)
            assert final_disk["review_rejected"] is False
            assert final_disk["pending_review_revision"] == 4
            assert final_disk["pending_clarifications"] == {}
            assert final_disk["requests"][-1]["response"] == persisted_response(recovered_payload)


CASES = [
    ("commit_normal_partial_turn_reopen_file", case_commit_normal_partial_turn_reopen_file),
    ("stop_app_new_instance_same_directory_resume_partial", case_stop_app_new_instance_same_directory_resume_partial),
    ("scoped_os_replace_failure_during_turn_save", case_scoped_os_replace_failure_during_turn_save),
    ("scoped_create_write_replace_failure", case_scoped_create_write_replace_failure),
    ("corrupt_existing_session_json", case_corrupt_existing_session_json),
    ("persisted_future_schema_version", case_persisted_future_schema_version),
    ("path_like_session_id_through_public_route", case_path_like_session_id_through_public_route),
    ("resume_rejected_session_pending_issue", case_resume_rejected_session_pending_issue),
]


@pytest.mark.parametrize(
    "case_id,case", CASES, ids=[case_id for case_id, _ in CASES]
)
async def test_persistence_and_restart(
    case_id, case, settings, provider, session_dir
):
    await case(settings, provider, session_dir)
