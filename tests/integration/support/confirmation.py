"""Shared helpers for the confirmation and rejection state tests: scripted
extractions, session factories and commit/ledger assertions."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from gateway.localization import request_reference
from tests.integration.support.api import (
    ScriptedProvider,
    make_extraction,
    persisted_response,
    read_session,
)
from tests.integration.support.intake import (
    FULL_SLOTS,
    FULL_TEXT,
    REJECT_PROMPT,
    full_extraction,
    post_turn,
    start_session,
    turn_body,
)

YES_TEXT = "Yes."

NO_TEXT = "No."

CHANGE_TEXT = "Actually make the urgency low."

RETURN_Q_TEXT = "Can I return an unused item?"

POLICY_QUESTION_SUFFIX = "Please ask your policy question in a separate message."

RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)

CONFIRM_USER_TEXT = "Confirm these details"

STALE_REVIEW_MESSAGE = "The review changed. Review the latest details before confirming."

REVIEW_NOT_READY_MESSAGE = "Resolve missing or unclear details before confirming."

LOW_SUMMARY = (
    "Account reference: NS-000123\n"
    "Energy support area: equipment issue\n"
    "Issue: My order arrived broken\n"
    "Urgency: low"
)

AMBIGUOUS_URGENCY = "Please clarify your urgency."


def receipt_for(session_id: str) -> str:
    return f"Saved locally as {request_reference(session_id)}. No request has been sent to a real support team. Is there anything else I can help with?"


def reject_extraction():
    """NO: reject with no facts."""

    return make_extraction(intent="reject")


def yes_extraction():
    """YES: unambiguous affirmation with no candidates."""

    return make_extraction(intent="confirm")


def yes_equal_extraction():
    """YES restating every accepted value."""

    return make_extraction(intent="confirm", candidates=dict(FULL_SLOTS))


def yes_changed_urgency_extraction():
    """YES with a correction: a changed urgency is an explicit correction."""

    return make_extraction(
        intent="correction",
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )


def yes_policy_question_extraction():
    """A YES carrying a policy question is a question, not an affirmation."""

    return make_extraction(intent="question", has_question=True)


def equal_explicit_correction_extraction():
    return make_extraction(
        intent="correction",
        candidates={"urgency": "high"},
        explicit_corrections=["urgency"],
    )


def invalid_correction_extraction():
    return make_extraction(
        intent="correction",
        candidates={"order_number": "NS-12345"},
        explicit_corrections=["order_number"],
    )


def valid_correction_plus_invalid_sibling_extraction():
    return make_extraction(
        intent="correction",
        candidates={"urgency": "low", "order_number": "NS-12345"},
        explicit_corrections=["urgency", "order_number"],
    )


def unrelated_extraction():
    return make_extraction(intent="unrelated")


def reject_with_correction_extraction():
    return make_extraction(
        intent="reject",
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )


def correction_with_question_extraction():
    return make_extraction(
        intent="correction",
        has_question=True,
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )


def ambiguous_urgency_extraction():
    return make_extraction(candidates={}, ambiguous_fields=["urgency"])


def change_extraction():
    return make_extraction(
        intent="correction",
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )


def assert_session_state(
    view: dict,
    *,
    revision: int,
    status: str,
    slots: dict,
    summary: str | None,
    token: int | None,
    pending: dict | None = None,
) -> None:
    assert view["revision"] == revision
    assert view["status"] == status
    assert view["slots"] == slots
    assert view["summary"] == summary
    assert view["pending_review_revision"] == token
    assert view["pending_clarifications"] == (pending or {})


def assert_ledger(
    provider: ScriptedProvider,
    *,
    calls_before: int,
    extracts: int,
    answers: int,
    context: dict | None = None,
) -> None:
    """Count logical provider calls for one action; no unexpected method."""

    new_calls = provider.calls[calls_before:]
    names = [name for name, _ in new_calls]
    assert names.count("extract") == extracts
    assert names.count("answer_knowledge") == answers
    assert len(names) == extracts + answers
    if context is not None:
        extract_calls = [call for name, call in new_calls if name == "extract"]
        assert len(extract_calls) == 1
        sent = extract_calls[0]
        assert sent.current_message == context["current_message"]
        assert sent.slots.model_dump() == context["slots"]
        assert {
            field: clarification.model_dump()
            for field, clarification in sent.pending_clarifications.items()
        } == context["pending_clarifications"]
        assert sent.status == context["status"]


def assert_turn_commit(
    *,
    session_dir: Path,
    session_id: str,
    before: dict,
    response: dict,
    request_id: str,
    text: str,
    counter_delta: int,
) -> dict:
    """One committed ordinary turn: pair, replay record and revision."""

    disk = read_session(session_dir, session_id)
    assert disk["revision"] == before["revision"] + 1 == response["revision"]
    assert disk["collection_turn_count"] == before["collection_turn_count"] + counter_delta
    assert disk["messages"][:-2] == before["messages"]
    assert len(disk["messages"]) == len(before["messages"]) + 2
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
    assert disk["requests"][:-1] == before["requests"]
    assert len(disk["requests"]) == len(before["requests"]) + 1
    assert disk["requests"][-1]["request_id"] == request_id
    assert disk["requests"][-1]["response"] == persisted_response(response)
    for key in (
        "status",
        "slots",
        "pending_clarifications",
        "pending_review_revision",
        "summary",
    ):
        assert disk[key] == response[key]
    return disk


async def post_confirm(
    client,
    session_id: str,
    *,
    expected_revision: int,
    review_revision: int,
    request_id: str | None = None,
):
    body = {
        "request_id": request_id or str(uuid4()),
        "expected_revision": expected_revision,
        "review_revision": review_revision,
    }
    response = await client.post(f"/api/sessions/{session_id}/confirm", json=body)
    return body["request_id"], response


async def send_turn(
    client, session_id: str, *, text: str, revision: int
):
    """Post a real turn request and keep its request_id for the replay check."""

    request_id = str(uuid4())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(text, revision, request_id),
    )
    return request_id, response


async def assert_error(
    response, *, status: int, code: str, message: str, current_revision: int | None = None
) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    expected = {"code": code, "message": message, "retryable": False}
    if current_revision is not None:
        expected["current_revision"] = current_revision
    assert body["error"] == expected
    assert set(body) == {"error", "request_id"}
    return body


def assert_dedicated_confirm_commit(
    *,
    session_dir: Path,
    session_id: str,
    before: dict,
    payload: dict,
    request_id: str,
    review_revision: int,
) -> dict:
    """The dedicated confirm transaction: receipt pair, no counter increment."""

    receipt = receipt_for(session_id)
    assert payload["status"] == "completed"
    assert payload["revision"] == before["revision"] + 1
    assert payload["reply"] == receipt
    assert payload["slots"] == before["slots"]
    assert payload["summary"] == before["summary"]
    assert payload["pending_clarifications"] == {}
    assert payload["pending_review_revision"] is None
    assert payload["citations"] == []
    disk = read_session(session_dir, session_id)
    assert disk["revision"] == payload["revision"]
    assert disk["status"] == "completed"
    assert disk["collection_turn_count"] == before["collection_turn_count"]
    assert disk["summary"] == before["summary"]
    assert disk["pending_review_revision"] is None
    assert disk["pending_clarifications"] == {}
    assert disk["confirmation"]["review_revision"] == review_revision
    assert disk["confirmation"]["confirmed_at"].endswith("Z")
    assert disk["messages"][:-2] == before["messages"]
    assert len(disk["messages"]) == len(before["messages"]) + 2
    user, assistant = disk["messages"][-2:]
    assert user["role"] == "user"
    assert user["text"] == CONFIRM_USER_TEXT
    assert user["input_mode"] == "system"
    assert user["citations"] == []
    assert assistant["role"] == "assistant"
    assert assistant["input_mode"] == "system"
    assert assistant["id"] == payload["message_id"]
    assert assistant["text"] == receipt
    assert assistant["citations"] == []
    assert disk["requests"][:-1] == before["requests"]
    assert len(disk["requests"]) == len(before["requests"]) + 1
    assert disk["requests"][-1]["request_id"] == request_id
    assert disk["requests"][-1]["response"] == {key: value for key, value in payload.items() if key != "speech_available"}
    return disk


async def review_session(client, provider) -> str:
    """One FULL turn reaches a fresh review: revision 1, token 1, counter 1."""

    session_id = await start_session(client)
    await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    return session_id


async def rejected_session(client, provider) -> str:
    """review → NO: review_rejected=true, collecting, token/summary null at revision 2."""

    session_id = await review_session(client, provider)
    provider.script_extraction(reject_extraction())
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body(NO_TEXT, 1),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_session_state(
        payload,
        revision=2,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == REJECT_PROMPT
    return session_id
