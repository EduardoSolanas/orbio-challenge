"""IT01-01..IT01-23: HTTP contract, request guards and correlation IDs.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is
a scripted fake (IC §13.1, §13.2 IT01).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.errors import ERRORS
from app.prompts import CAPABILITY, GREETING
from tests.integration.support_api import (
    ScriptedProvider,
    full_extraction,
    raw_asgi,
    read_session,
    session_files,
    unrelated_extraction,
)

SESSION_VIEW_KEYS = {
    "session_id",
    "revision",
    "status",
    "reply_language",
    "slots",
    "messages",
    "pending_clarifications",
    "pending_review_revision",
    "summary",
}

TURN_RESPONSE_KEYS = {
    "session_id",
    "revision",
    "status",
    "reply_language",
    "reply",
    "message_id",
    "speech_available",
    "slots",
    "pending_clarifications",
    "pending_review_revision",
    "summary",
    "citations",
}

MESSAGE_VIEW_KEYS = {
    "id",
    "role",
    "text",
    "input_mode",
    "created_at",
    "citations",
    "language",
    "speech_available",
}

STORED_MESSAGE_KEYS = MESSAGE_VIEW_KEYS - {"speech_available"}


def turn_body(
    *,
    request_id: str | None = None,
    expected_revision: int = 0,
    text: str = "My order arrived broken.",
    input_mode: str = "text",
    **extra: object,
) -> dict:
    return {
        "request_id": request_id or str(uuid4()),
        "expected_revision": expected_revision,
        "text": text,
        "input_mode": input_mode,
        **extra,
    }


def assert_error(response, code: str) -> dict:
    spec = ERRORS[code]
    assert response.status_code == spec.status
    payload = response.json()
    assert payload["error"] == {
        "code": code,
        "message": spec.message,
        "retryable": spec.retryable,
    }
    UUID(payload["request_id"])
    return payload


def assert_no_calls(provider: ScriptedProvider) -> None:
    assert provider.calls == []


async def case_it01_01(client, app, provider, session_dir):
    response = await client.post("/api/sessions", content=b"")
    assert response.status_code == 201
    view = response.json()
    assert set(view) == SESSION_VIEW_KEYS
    assert view["revision"] == 0
    assert view["status"] == "collecting"
    assert view["slots"] == {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert view["pending_clarifications"] == {}
    assert view["pending_review_revision"] is None
    assert view["summary"] is None
    assert [message["role"] for message in view["messages"]] == ["assistant"]
    assert view["messages"][0]["text"] == GREETING
    assert view["messages"][0]["input_mode"] == "system"
    assert view["messages"][0]["citations"] == []
    files = session_files(session_dir)
    assert [path.name for path in files] == [f"{view['session_id']}.json"]
    assert_no_calls(provider)
    disk = read_session(session_dir, view["session_id"])
    assert disk["revision"] == 0
    assert disk["requests"] == []
    assert len(disk["messages"]) == 1


async def case_it01_02(client, app, provider, session_dir):
    response = await client.post("/api/sessions", json={})
    assert response.status_code == 201
    view = response.json()
    assert view["revision"] == 0
    assert view["status"] == "collecting"
    assert [message["role"] for message in view["messages"]] == ["assistant"]
    assert view["messages"][0]["text"] == GREETING
    assert len(session_files(session_dir)) == 1
    assert_no_calls(provider)


async def case_it01_03(client, app, provider, session_dir):
    response = await client.post("/api/sessions", json={"unexpected": 1})
    assert_error(response, "invalid_request")
    assert session_files(session_dir) == []
    assert_no_calls(provider)


async def case_it01_04(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.get(f"/api/sessions/{session_id}")
    assert response.status_code == 200
    view = response.json()
    assert set(view) == SESSION_VIEW_KEYS
    disk = json.loads(before.decode("utf-8"))
    for key in SESSION_VIEW_KEYS:
        if key == "messages":
            # The view adds the configuration-derived playback flag per message;
            # every stored field must still match byte-for-byte.
            assert len(view["messages"]) == len(disk["messages"])
            for served, stored in zip(view["messages"], disk["messages"]):
                assert set(served) == MESSAGE_VIEW_KEYS
                assert {k: served[k] for k in STORED_MESSAGE_KEYS} == stored
                assert served["speech_available"] is True
            continue
        assert view[key] == disk[key], key
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_05(client, app, provider, session_dir):
    response = await client.get(f"/api/sessions/{uuid4()}")
    assert_error(response, "session_not_found")
    assert session_files(session_dir) == []
    assert_no_calls(provider)


async def case_it01_09(client, app, provider, session_dir):
    response = await client.get("/api/sessions/not-a-uuid")
    assert_error(response, "invalid_request")
    assert session_files(session_dir) == []
    assert_no_calls(provider)


async def case_it01_06(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(extra_key=True)
    )
    assert_error(response, "invalid_request")
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_07(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    provider.script_extraction(full_extraction())
    reviewed = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body()
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "awaiting_confirmation"
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/confirm",
        json={
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "review_revision": 1,
            "unexpected": True,
        },
    )
    assert_error(response, "invalid_request")
    assert path.read_bytes() == before
    assert provider.methods() == ["extract"]


async def case_it01_10(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(expected_revision=True),
    )
    assert_error(response, "invalid_request")
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_11(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(expected_revision=-1),
    )
    assert_error(response, "invalid_request")
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_12(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text="   ")
    )
    assert_error(response, "invalid_request")
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_13(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    provider.script_extraction(unrelated_extraction())
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text="x" * 4000)
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == TURN_RESPONSE_KEYS
    assert body["reply"] == CAPABILITY
    assert body["revision"] == 1
    assert body["status"] == "collecting"
    assert body["slots"] == {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert body["citations"] == []
    UUID(body["message_id"])
    assert provider.methods() == ["extract"]
    disk = read_session(session_dir, session_id)
    assert disk["revision"] == 1
    assert len(disk["messages"]) == 3
    assert disk["messages"][1]["text"] == "x" * 4000
    assert len(disk["requests"]) == 1
    assert disk["collection_turn_count"] == 1


async def case_it01_14(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text="x" * 4001)
    )
    assert_error(response, "input_too_large")
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_15(client, app, provider, session_dir):
    body = b"{}" + b" " * (65536 - 2)
    assert len(body) == 65536
    response = await client.post(
        "/api/sessions", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 201
    assert len(session_files(session_dir)) == 1
    assert_no_calls(provider)


async def case_it01_16(client, app, provider, session_dir):
    body = b"{}" + b" " * (65537 - 2)
    assert len(body) == 65537
    response = await client.post(
        "/api/sessions", content=body, headers={"content-type": "application/json"}
    )
    assert_error(response, "input_too_large")
    assert session_files(session_dir) == []
    assert_no_calls(provider)


async def case_it01_17(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    status, headers, body = await raw_asgi(
        app,
        method="POST",
        path=f"/api/sessions/{session_id}/turns",
        chunks=[b'{"request_id": "' + b"x" * 70000],
        headers={"content-type": "application/json", "content-length": "10"},
    )
    assert status == 413
    payload = json.loads(body)
    assert payload["error"] == {
        "code": "input_too_large",
        "message": ERRORS["input_too_large"].message,
        "retryable": False,
    }
    UUID(payload["request_id"])
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_18(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    status, headers, body = await raw_asgi(
        app,
        method="POST",
        path=f"/api/sessions/{session_id}/turns",
        chunks=[b'{"request_id": "' + b"x" * 70000],
        headers={"content-type": "application/json"},
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == "input_too_large"
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_19(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    provider.script_extraction(full_extraction())
    reviewed = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body()
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["pending_review_revision"] == 1
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    status, headers, body = await raw_asgi(
        app,
        method="POST",
        path=f"/api/sessions/{session_id}/confirm",
        chunks=[b'{"request_id": "' + b"x" * 70000],
        headers={"content-type": "application/json"},
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == "input_too_large"
    assert path.read_bytes() == before
    assert provider.methods() == ["extract"]


async def case_it01_21(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    text = "Café ☕ naïve — my order arrived broken."
    payload = json.dumps(turn_body(text=text), ensure_ascii=False).encode("utf-8")
    body = payload + b" " * (65536 - len(payload))
    assert len(body) == 65536
    provider.script_extraction(unrelated_extraction())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert provider.methods() == ["extract"]
    disk = read_session(session_dir, session_id)
    assert disk["revision"] == 1
    assert disk["messages"][1]["text"] == text
    assert len(disk["messages"]) == 3


async def case_it01_22(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    correlation = str(uuid4())
    body = turn_body()
    provider.script_extraction(unrelated_extraction())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=body,
        headers={"X-Request-ID": correlation},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == correlation
    assert provider.methods() == ["extract"]
    replayed = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=body,
        headers={"X-Request-ID": str(uuid4())},
    )
    assert replayed.status_code == 200
    assert replayed.json() == response.json()
    assert provider.methods() == ["extract"]


async def case_it01_23(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    body = turn_body()
    provider.script_extraction(unrelated_extraction())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=body,
        headers={"X-Request-ID": "not-a-uuid"},
    )
    assert response.status_code == 200
    correlation = response.headers["x-request-id"]
    assert UUID(correlation)
    assert correlation != "not-a-uuid"
    replayed = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=body,
        headers={"X-Request-ID": "also-not-a-uuid"},
    )
    assert replayed.status_code == 200
    assert replayed.json() == response.json()
    assert UUID(replayed.headers["x-request-id"])
    assert provider.methods() == ["extract"]


async def case_it01_08(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    message_id = created["messages"][0]["id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    response = await client.post(
        f"/api/sessions/{session_id}/speech",
        json={"message_id": message_id, "text": "Caller supplied speech"},
    )
    assert_error(response, "invalid_request")
    assert path.read_bytes() == before
    assert_no_calls(provider)


async def case_it01_20(client, app, provider, session_dir):
    created = (await client.post("/api/sessions", json={})).json()
    session_id = created["session_id"]
    message_id = created["messages"][0]["id"]
    path = Path(session_dir) / f"{session_id}.json"
    before = path.read_bytes()
    status, headers, body = await raw_asgi(
        app,
        method="POST",
        path=f"/api/sessions/{session_id}/speech",
        chunks=[
            b'{"message_id": "' + message_id.encode("ascii") + b'", "pad": "'
            + b"x" * 70000
        ],
        headers={"content-type": "application/json"},
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == "input_too_large"
    assert path.read_bytes() == before
    assert_no_calls(provider)


CASES = [
    pytest.param(case_it01_01, id="IT01-01"),
    pytest.param(case_it01_02, id="IT01-02"),
    pytest.param(case_it01_03, id="IT01-03"),
    pytest.param(case_it01_04, id="IT01-04"),
    pytest.param(case_it01_05, id="IT01-05"),
    pytest.param(case_it01_06, id="IT01-06"),
    pytest.param(case_it01_07, id="IT01-07"),
    pytest.param(case_it01_08, id="IT01-08"),
    pytest.param(case_it01_09, id="IT01-09"),
    pytest.param(case_it01_10, id="IT01-10"),
    pytest.param(case_it01_11, id="IT01-11"),
    pytest.param(case_it01_12, id="IT01-12"),
    pytest.param(case_it01_13, id="IT01-13"),
    pytest.param(case_it01_14, id="IT01-14"),
    pytest.param(case_it01_15, id="IT01-15"),
    pytest.param(case_it01_16, id="IT01-16"),
    pytest.param(case_it01_17, id="IT01-17"),
    pytest.param(case_it01_18, id="IT01-18"),
    pytest.param(case_it01_19, id="IT01-19"),
    pytest.param(case_it01_20, id="IT01-20"),
    pytest.param(case_it01_21, id="IT01-21"),
    pytest.param(case_it01_22, id="IT01-22"),
    pytest.param(case_it01_23, id="IT01-23"),
]


@pytest.mark.parametrize("case", CASES)
async def test_it01_http_contract(case, client, app, provider, session_dir):
    await case(client, app, provider, session_dir)
