"""IT02-01..IT02-10: intake lifecycle, merge order and corrections.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is
a scripted fake (IC §13.1, §13.2 IT02). Fixture strings are the literal IC §13.1
scripted inputs.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.support_api import (
    ScriptedProvider,
    make_extraction,
    read_session,
)

PART_TEXT = "My order NS-000123 arrived broken."
CATEGORY_TEXT = "It is a damaged item."
HIGH_TEXT = "High urgency."
FULL_TEXT = "My order NS-000123 arrived broken. It is a damaged item and high urgency."
CHANGE_TEXT = "Actually make the urgency low."

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


def change_extraction():
    return make_extraction(
        intent="correction",
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )


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


async def case_it02_01(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_clarifications"] == {}
    assert response["revision"] == 1
    assert response["pending_review_revision"] == response["revision"] == 1
    assert response["summary"] == FULL_SUMMARY
    assert response["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert response["citations"] == []
    assert provider.methods() == ["extract"]
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )


async def case_it02_02(client, provider, session_dir):
    session_id = await start_session(client)
    calls = 0
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=HIGH_TEXT,
        revision=0,
        extraction=high_extraction(),
    )
    assert response["slots"] == {**EMPTY_SLOTS, "urgency": "high"}
    assert response["reply"] == MISSING_ORDER
    assert response["pending_clarifications"] == {}
    assert response["status"] == "collecting"
    assert response["pending_review_revision"] is None
    assert response["summary"] is None
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=calls,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=HIGH_TEXT,
    )
    calls += 1

    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=CATEGORY_TEXT,
        revision=1,
        extraction=category_extraction(),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "category": "damaged_item",
        "urgency": "high",
    }
    assert response["reply"] == MISSING_ORDER
    assert response["status"] == "collecting"
    assert response["pending_review_revision"] is None
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=calls,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=CATEGORY_TEXT,
    )
    calls += 1

    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=2,
        extraction=part_extraction(),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY
    assert response["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=calls,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=PART_TEXT,
    )
    assert provider.methods() == ["extract", "extract", "extract"]


async def case_it02_03(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "order_number": "NS-000123",
        "description": "My order arrived broken",
    }
    assert response["reply"] == MISSING_CATEGORY
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=PART_TEXT,
    )

    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=1,
        extraction=part_extraction(),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "order_number": "NS-000123",
        "description": "My order arrived broken",
    }
    assert response["pending_clarifications"] == {}
    assert response["reply"] == MISSING_CATEGORY
    assert response["revision"] == 2
    disk = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=PART_TEXT,
    )
    assert len(disk["messages"]) == 5
    assert len(disk["requests"]) == 2
    assert provider.methods() == ["extract", "extract"]


async def case_it02_04(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    text = "My order NS-12345 arrived broken. It is high urgency; can I return it?"
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            has_question=True,
            candidates={
                "order_number": "NS-12345",
                "description": "  My order arrived broken  ",
                "urgency": "high",
            },
        ),
    )
    assert response["slots"] == {
        "order_number": None,
        "category": None,
        "description": "My order arrived broken",
        "urgency": "high",
    }
    assert response["pending_clarifications"] == {
        "order_number": {
            "code": "invalid",
            "candidate": "NS-12345",
            "question": INVALID_ORDER,
        }
    }
    assert response["reply"] == (
        f"{INVALID_ORDER} Please ask your policy question in a separate message."
    )
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it02_05(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["pending_review_revision"] == 1
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    low_summary = FULL_SUMMARY.replace("Urgency: high", "Urgency: low")
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=CHANGE_TEXT,
        revision=1,
        extraction=change_extraction(),
    )
    assert response["slots"] == {**FULL_SLOTS, "urgency": "low"}
    assert response["status"] == "awaiting_confirmation"
    assert response["status"] != "completed"
    assert response["pending_clarifications"] == {}
    assert response["pending_review_revision"] == 2
    assert response["summary"] == low_summary
    assert response["reply"] == low_summary + REVIEW_SUFFIX
    disk = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=CHANGE_TEXT,
    )
    assert disk["status"] == "awaiting_confirmation"
    assert disk["summary"] == low_summary


async def case_it02_06(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    text = "My energy account reference is NS-654321."
    conflict_question = (
        "You previously gave NS-000123. "
        "Should I keep that or use NS-654321 for energy account reference?"
    )
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(candidates={"order_number": "NS-654321"}),
    )
    assert response["slots"] == FULL_SLOTS
    assert_issue(response, "order_number", "conflict", "NS-654321", conflict_question)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert response["revision"] == 2
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it02_07(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My energy account reference is NS-654321.",
        revision=1,
        extraction=make_extraction(candidates={"order_number": "NS-654321"}),
    )
    assert response["pending_clarifications"]["order_number"]["code"] == "conflict"
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My energy account reference is NS-654321.",
    )

    text = "Use NS-654321."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=2,
        extraction=make_extraction(
            intent="correction",
            candidates={"order_number": "NS-654321"},
            explicit_corrections=["order_number"],
        ),
    )
    assert response["slots"] == {**FULL_SLOTS, "order_number": "NS-654321"}
    assert response["pending_clarifications"] == {}
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY.replace(
        "Account reference: NS-000123", "Account reference: NS-654321"
    )
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=2,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )
    assert provider.methods() == ["extract", "extract", "extract"]


async def case_it02_08(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My energy account reference is NS-654321.",
        revision=1,
        extraction=make_extraction(candidates={"order_number": "NS-654321"}),
    )
    assert response["pending_clarifications"]["order_number"]["code"] == "conflict"
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My energy account reference is NS-654321.",
    )

    text = "Keep NS-000123."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=2,
        extraction=make_extraction(
            intent="correction",
            candidates={"order_number": "NS-000123"},
            explicit_corrections=["order_number"],
        ),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["pending_clarifications"] == {}
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=2,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it02_09(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["pending_review_revision"] == 1
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    text = "I am not sure about the urgency."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(
            candidates={}, ambiguous_fields=["urgency"]
        ),
    )
    assert response["slots"] == FULL_SLOTS
    assert_issue(
        response, "urgency", "ambiguous", None, "Please clarify your urgency."
    )
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it02_10(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My energy account reference is NS-12345.",
        revision=0,
        extraction=make_extraction(candidates={"order_number": "NS-12345"}),
    )
    assert_issue(response, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My energy account reference is NS-12345.",
    )

    text = "Thanks, that is all for now."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(candidates={}),
    )
    assert response["slots"] == EMPTY_SLOTS
    assert_issue(response, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    disk = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )
    assert disk["pending_clarifications"] == {
        "order_number": {
            "code": "invalid",
            "candidate": "NS-12345",
            "question": INVALID_ORDER,
        }
    }


CASES = [
    pytest.param(case_it02_01, id="IT02-01"),
    pytest.param(case_it02_02, id="IT02-02"),
    pytest.param(case_it02_03, id="IT02-03"),
    pytest.param(case_it02_04, id="IT02-04"),
    pytest.param(case_it02_05, id="IT02-05"),
    pytest.param(case_it02_06, id="IT02-06"),
    pytest.param(case_it02_07, id="IT02-07"),
    pytest.param(case_it02_08, id="IT02-08"),
    pytest.param(case_it02_09, id="IT02-09"),
    pytest.param(case_it02_10, id="IT02-10"),
]


@pytest.mark.parametrize("case", CASES)
async def test_it02_intake_lifecycle(case, client, provider, session_dir):
    await case(client, provider, session_dir)
