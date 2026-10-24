"""IT16: version-2 persistence and legacy migration over real HTTP routes.

Every case runs the real app factory, lifespan, routes, engine and JSON
repository against an isolated temporary SESSION_DIR. Legacy documents are
written straight into that directory, so the read path must migrate them in
memory without touching the original bytes; only a successful authorized turn
may replace the file with a version-2 document.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.schemas import Session
from tests.integration.support_api import make_extraction, session_files
from tests.integration.test_it02_intake import turn_body

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sessions"
V1_FIXTURES = [
    "v1-collecting.json",
    "v1-clarification.json",
    "v1-rejected.json",
    "v1-review.json",
    "v1-completed.json",
]
STORAGE_UNAVAILABLE_TEXT = "Session storage is unavailable. Please retry."
STORAGE_CORRUPT_TEXT = "This session cannot be loaded. Contact the demo operator."


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_document(session_dir: Path, document: dict) -> Path:
    path = session_dir / f"{document['session_id']}.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def assert_error_envelope(response, *, status, code, message, retryable):
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
    leftovers = [p.name for p in session_dir.iterdir() if not p.name.endswith(".json")]
    assert leftovers == []


@pytest.mark.parametrize("name", V1_FIXTURES)
async def test_get_migrates_version_one_in_memory_and_keeps_the_bytes(
    settings, provider, session_dir, name
):
    document = load_fixture(name)
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    before = path.read_bytes()
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/sessions/{document['session_id']}")

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["session_id"] == document["session_id"]
    assert view["revision"] == document["revision"]
    assert view["status"] == document["status"]
    assert view["reply_language"] == "en"
    assert view["frustration_active"] is False
    assert view["slots"] == document["slots"]
    assert view["pending_clarifications"] == document["pending_clarifications"]
    assert view["pending_review_revision"] == document["pending_review_revision"]
    assert view["summary"] == document["summary"]
    assert [message["id"] for message in view["messages"]] == [
        message["id"] for message in document["messages"]
    ]
    for message in view["messages"]:
        assert message["language"] == "en"
        assert message["sentiment"] is None
        assert message["frustration"] is None
        assert isinstance(message["speech_available"], bool)
    assert path.read_bytes() == before
    assert provider.calls == []
    assert session_files(session_dir) == [path]


async def test_get_fills_pre_extension_version_two_without_rewriting(
    settings, provider, session_dir
):
    document = load_fixture("v2-pre-extension.json")
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    before = path.read_bytes()
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/sessions/{document['session_id']}")

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["frustration_active"] is False
    assert view["reply_language"] == "en"
    assert all(message["sentiment"] is None for message in view["messages"])
    assert path.read_bytes() == before
    assert provider.calls == []


async def test_get_preserves_stored_labels_of_a_complete_version_two_file(
    settings, provider, session_dir
):
    document = load_fixture("v2-active-frustration-es.json")
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    before = path.read_bytes()
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/sessions/{document['session_id']}")

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["frustration_active"] is True
    assert view["reply_language"] == "es"
    assert view["messages"][1]["sentiment"] == "negative"
    assert view["messages"][1]["frustration"] == "present"
    assert view["messages"][0]["sentiment"] is None
    assert path.read_bytes() == before
    assert provider.calls == []


async def test_successful_turn_after_migration_writes_a_version_two_document(
    settings, provider, session_dir
):
    document = load_fixture("v1-collecting.json")
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    extraction = make_extraction(
        candidates={"description": "My order arrived broken"},
        sentiment="negative",
        frustration="present",
    )
    provider.script_extraction(extraction)
    app = create_app(settings=settings, provider=provider)
    body = turn_body(
        "My order arrived broken and I am frustrated.", revision=document["revision"]
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/sessions/{document['session_id']}/turns", json=body
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["revision"] == document["revision"] + 1
    assert payload["frustration_active"] is True
    assert payload["status"] == "handed_off"
    assert payload["reply"] == (
        "I understand this has been frustrating. This demo simulates a transfer "
        "to a human support agent. No live agent is connected, and no real "
        "support request has been sent."
    )
    assert payload["reply_language"] == "en"

    saved = json.loads(path.read_text(encoding="utf-8"))
    Session.model_validate(saved)
    assert saved["schema_version"] == 2
    assert saved["frustration_active"] is True
    assert saved["session_id"] == document["session_id"]
    assert saved["created_at"] == document["created_at"]
    assert saved["revision"] == 1
    assert saved["messages"][0]["id"] == document["messages"][0]["id"]
    assert saved["messages"][0]["text"] == document["messages"][0]["text"]
    assert saved["messages"][0]["sentiment"] is None
    assert saved["messages"][-2]["role"] == "user"
    assert saved["messages"][-2]["sentiment"] == "negative"
    assert saved["messages"][-2]["frustration"] == "present"
    assert saved["messages"][-1]["role"] == "assistant"
    assert saved["messages"][-1]["sentiment"] is None
    assert saved["messages"][-1]["frustration"] is None
    assert len(saved["requests"]) == 1
    assert saved["requests"][0]["response"]["frustration_active"] is True
    assert provider.methods() == ["extract"]
    assert_no_temp_files(session_dir)


async def test_failed_atomic_replace_preserves_the_original_version_one_file(
    settings, provider, session_dir, monkeypatch
):
    import app.storage as storage_module

    document = load_fixture("v1-collecting.json")
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    before = path.read_bytes()
    extraction = make_extraction(
        candidates={"description": "My order arrived broken"},
        sentiment="negative",
        frustration="present",
    )
    provider.script_extraction(extraction)
    app = create_app(settings=settings, provider=provider)
    real_replace = os.replace

    def failing_replace(src, dst):
        if str(dst).endswith(f"{document['session_id']}.json"):
            raise OSError("replace failed")
        real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)
    body = turn_body(
        "My order arrived broken and I am frustrated.", revision=document["revision"]
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/sessions/{document['session_id']}/turns", json=body
            )

    assert_error_envelope(
        response,
        status=503,
        code="storage_unavailable",
        message=STORAGE_UNAVAILABLE_TEXT,
        retryable=True,
    )
    assert path.read_bytes() == before
    assert session_files(session_dir) == [path]
    assert_no_temp_files(session_dir)


async def test_successful_turn_on_pre_extension_version_two_fills_the_new_fields(
    settings, provider, session_dir
):
    document = load_fixture("v2-pre-extension.json")
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    provider.script_extraction(
        make_extraction(
            candidates={"order_number": "NS-000123"},
            sentiment="neutral",
            frustration="none",
        )
    )
    app = create_app(settings=settings, provider=provider)
    body = turn_body("Order NS-000123.", revision=document["revision"])

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/sessions/{document['session_id']}/turns", json=body
            )

    assert response.status_code == 200, response.text
    saved = json.loads(path.read_text(encoding="utf-8"))
    Session.model_validate(saved)
    assert saved["schema_version"] == 2
    assert saved["frustration_active"] is False
    assert saved["reply_language"] == "en"
    assert saved["messages"][1]["sentiment"] is None
    assert saved["messages"][1]["frustration"] is None
    assert saved["messages"][-2]["sentiment"] == "neutral"
    assert saved["messages"][-2]["frustration"] == "none"
    assert saved["requests"][0]["response"]["frustration_active"] is False
    assert saved["requests"][-1]["response"]["frustration_active"] is False


async def test_get_rejects_a_corrupt_file_without_overwriting_it(
    settings, provider, session_dir
):
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id = "88888888-8888-4888-8888-888888888888"
    path = session_dir / f"{session_id}.json"
    path.write_text("{not json", encoding="utf-8")
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/sessions/{session_id}")

    assert_error_envelope(
        response,
        status=503,
        code="storage_corrupt",
        message=STORAGE_CORRUPT_TEXT,
        retryable=False,
    )
    assert path.read_text(encoding="utf-8") == "{not json"
    assert provider.calls == []


@pytest.mark.parametrize("version", [3, 0], ids=str)
async def test_unknown_schema_versions_remain_errors(
    settings, provider, session_dir, version
):
    document = load_fixture("v2-active-frustration-es.json")
    document["schema_version"] = version
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    before = path.read_bytes()
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            get_response = await client.get(f"/api/sessions/{document['session_id']}")
            turn_response = await client.post(
                f"/api/sessions/{document['session_id']}/turns",
                json=turn_body("Hello.", revision=document["revision"]),
            )

    for response in (get_response, turn_response):
        assert_error_envelope(
            response,
            status=503,
            code="storage_corrupt",
            message=STORAGE_CORRUPT_TEXT,
            retryable=False,
        )
    assert path.read_bytes() == before
    assert provider.calls == []


async def test_invalid_stored_label_is_corruption_not_neutral(
    settings, provider, session_dir
):
    document = load_fixture("v2-active-frustration-es.json")
    document["messages"][1]["sentiment"] = "furious"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = write_document(session_dir, document)
    before = path.read_bytes()
    app = create_app(settings=settings, provider=provider)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/sessions/{document['session_id']}")

    assert_error_envelope(
        response,
        status=503,
        code="storage_corrupt",
        message=STORAGE_CORRUPT_TEXT,
        retryable=False,
    )
    assert path.read_bytes() == before
    assert provider.calls == []
