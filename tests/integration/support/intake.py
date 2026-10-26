"""Scripted extractions, constants and turn helpers for the intake-style tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from tests.integration.support.api import (
    ScriptedProvider,
    make_extraction,
    read_session,
)

PART_TEXT = "My order NS-000123 arrived broken."

CATEGORY_TEXT = "It is a damaged item."

HIGH_TEXT = "High urgency."

FULL_TEXT = "My order NS-000123 arrived broken. It is a damaged item and high urgency."

REVIEW_SUFFIX = "\nPlease confirm these details or tell me what to change."

MISSING_ORDER = (
    "What is your energy account reference? Use NS- followed by six digits, for example NS-123456."
)

MISSING_CATEGORY = (
    "Which area do you need help with: a bill, a supply issue, a meter reading, moving home, or something else?"
)

INVALID_ORDER = "Use NS- followed by exactly six digits for your energy account reference, for example NS-123456."

REJECT_PROMPT = "What would you like to change?"

EMPTY_SLOTS = {
    "order_number": None,
    "category": None,
    "description": None,
    "urgency": None,
}

FULL_SLOTS = {
    "order_number": "NS-000123",
    "category": "damaged_item",
    "description": "My order arrived broken",
    "urgency": "high",
}

FULL_SUMMARY = (
    "Account reference: NS-000123\n"
    "Energy support area: equipment issue\n"
    "Issue: My order arrived broken\n"
    "Urgency: high"
)


def part_extraction():
    return make_extraction(
        candidates={
            "order_number": "NS-000123",
            "description": "My order arrived broken",
        }
    )


def category_extraction():
    return make_extraction(candidates={"category": "damaged_item"})


def high_extraction():
    return make_extraction(candidates={"urgency": "high"})


def full_extraction():
    return make_extraction(candidates=dict(FULL_SLOTS))


def turn_body(text: str, revision: int, request_id: str | None = None) -> dict:
    return {
        "request_id": request_id or str(uuid4()),
        "expected_revision": revision,
        "text": text,
        "input_mode": "text",
    }


async def start_session(client) -> str:
    created = await client.post("/api/sessions", json={})
    assert created.status_code == 201
    return created.json()["session_id"]


async def post_turn(
    client,
    provider: ScriptedProvider,
    session_id: str,
    *,
    text: str,
    revision: int,
    extraction,
) -> tuple[str, dict]:
    provider.script_extraction(extraction)
    request_id = str(uuid4())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(text, revision, request_id),
    )
    assert response.status_code == 200, response.text
    return request_id, response.json()


def assert_provider_ledger(
    provider: ScriptedProvider, calls_before: int, expected_context: dict
) -> None:
    """Exactly one logical extract call, carrying the pre-turn context."""

    assert len(provider.calls) == calls_before + 1
    method, context = provider.calls[-1]
    assert method == "extract"
    assert context.current_message == expected_context["current_message"]
    assert context.slots.model_dump() == expected_context["slots"]
    assert {
        field: clarification.model_dump()
        for field, clarification in context.pending_clarifications.items()
    } == expected_context["pending_clarifications"]
    assert context.status == expected_context["status"]


def assert_normal_turn(
    *,
    session_dir: Path,
    session_id: str,
    provider: ScriptedProvider,
    calls_before: int,
    before_disk: dict,
    request_id: str,
    response: dict,
    text: str,
) -> dict:
    """One committed ordinary turn: pair, replay, revision and counter."""

    assert_provider_ledger(
        provider,
        calls_before,
        {
            "current_message": text,
            "slots": before_disk["slots"],
            "pending_clarifications": before_disk["pending_clarifications"],
            "status": before_disk["status"],
        },
    )
    disk = read_session(session_dir, session_id)
    assert disk["revision"] == before_disk["revision"] + 1 == response["revision"]
    assert disk["collection_turn_count"] == before_disk["collection_turn_count"] + 1
    assert disk["messages"][:-2] == before_disk["messages"]
    assert len(disk["messages"]) == len(before_disk["messages"]) + 2
    user, assistant = disk["messages"][-2:]
    assert user["role"] == "user"
    assert user["text"] == text
    assert user["input_mode"] == "text"
    assert user["citations"] == []
    assert assistant["role"] == "assistant"
    assert assistant["input_mode"] == "system"
    assert assistant["id"] == response["message_id"]
    assert assistant["text"] == response["reply"]
    assert assistant["citations"] == response["citations"]
    assert disk["requests"][:-1] == before_disk["requests"]
    assert len(disk["requests"]) == len(before_disk["requests"]) + 1
    record = disk["requests"][-1]
    assert record["request_id"] == request_id
    # ``speech_available`` is configuration-derived and never persisted, so the
    # stored replay record is the response without that serialization-only flag.
    assert record["response"] == {
        key: value for key, value in response.items() if key != "speech_available"
    }
    for key in (
        "status",
        "slots",
        "pending_clarifications",
        "pending_review_revision",
        "summary",
    ):
        assert disk[key] == response[key]
    return disk


def assert_issue(
    response: dict, field: str, code: str, candidate: str | None, question: str
) -> None:
    assert response["pending_clarifications"] == {
        field: {"code": code, "candidate": candidate, "question": question}
    }
    assert response["reply"] == question
