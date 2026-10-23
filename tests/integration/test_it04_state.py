"""IT04-01..IT04-19: confirmation and rejection state matrix.

Every case runs against the real app factory, real lifespan, real engine and a
real JSON repository in a temporary directory; only the AI service boundary is a
scripted fake (IC §13.1, §13.2 IT04). Each action is checked on the wire
(flag/token/summary/status/revision/messages) and in the committed temp JSON,
with the provider ledger counted before and after (E = extract, A =
answer_knowledge).

Fixture strings are the literal IC §13.1 scripted inputs. The shared turn
helpers live in `test_it02_intake.py` (ORB-07) so this card adds no support
module outside its owned files.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas import KnowledgeAnswer
from tests.integration.support_api import ScriptedProvider, make_extraction, read_session
from tests.integration.test_it02_intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    FULL_TEXT,
    INVALID_ORDER,
    REJECT_PROMPT,
    REVIEW_SUFFIX,
    assert_issue,
    full_extraction,
    part_extraction,
    post_turn,
    start_session,
    turn_body,
)

YES_TEXT = "Yes."
NO_TEXT = "No."
CHANGE_TEXT = "Actually make the urgency low."
RETURN_Q_TEXT = "Can I return an unused item?"
POLICY_QUESTION_SUFFIX = "Please ask your policy question in a separate message."
REVIEW_POLICY_FOLLOWUP = (
    "Please confirm the reviewed details or tell me what to change."
)
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)
CONFIRM_USER_TEXT = "Confirm reviewed intake"
STALE_REVIEW_MESSAGE = "The review changed. Review the latest details before confirming."
REVIEW_NOT_READY_MESSAGE = "Resolve missing or unclear details before confirming."
LOW_SUMMARY = (
    "Order: NS-000123\n"
    "Category: damaged_item\n"
    "Problem: My order arrived broken\n"
    "Urgency: low"
)
AMBIGUOUS_URGENCY = "Please clarify your urgency."


def receipt_for(session_id: str) -> str:
    return f"Saved locally as {session_id}. No request has been sent to a real support team."


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
    assert disk["requests"][-1]["response"] == response
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
    assert disk["requests"][-1]["response"] == payload
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


# IT04-01 -------------------------------------------------------------------


async def case_it04_01(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    assert_session_state(
        before,
        revision=1,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert before["review_rejected"] is False
    assert before["collection_turn_count"] == 1

    calls_before = len(provider.calls)
    request_id, response = await post_confirm(
        client, session_id, expected_revision=1, review_revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert_dedicated_confirm_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        payload=payload,
        request_id=request_id,
        review_revision=1,
    )


# IT04-02 -------------------------------------------------------------------


async def case_it04_02(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(
        provider,
        calls_before=calls_before,
        extracts=1,
        answers=0,
        context={
            "current_message": YES_TEXT,
            "slots": before["slots"],
            "pending_clarifications": {},
            "status": "awaiting_confirmation",
        },
    )
    receipt = receipt_for(session_id)
    assert_session_state(
        payload,
        revision=2,
        status="completed",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=None,
    )
    assert payload["reply"] == receipt
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=0,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"]["review_revision"] == 1
    assert disk["confirmation"]["confirmed_at"].endswith("Z")


# IT04-03 -------------------------------------------------------------------


async def case_it04_03(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_equal_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=2,
        status="completed",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=None,
    )
    assert payload["reply"] == receipt_for(session_id)
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=0,
    )
    assert disk["confirmation"]["review_revision"] == 1
    assert disk["review_rejected"] is False


# IT04-04 -------------------------------------------------------------------


async def case_it04_04(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_changed_urgency_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(
        provider,
        calls_before=calls_before,
        extracts=1,
        answers=0,
        context={
            "current_message": YES_TEXT,
            "slots": FULL_SLOTS,
            "pending_clarifications": {},
            "status": "awaiting_confirmation",
        },
    )
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=low_slots,
        summary=LOW_SUMMARY,
        token=2,
    )
    assert payload["reply"] == LOW_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["status"] == "awaiting_confirmation"
    assert disk["confirmation"] is None
    assert disk["review_rejected"] is False


# IT04-05 -------------------------------------------------------------------


async def case_it04_05(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_policy_question_extraction())
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )
    request_id, response = await send_turn(
        client, session_id, text=RETURN_Q_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=1)
    assert_session_state(
        payload,
        revision=2,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=1,
    )
    assert payload["reply"] == f"{RETURN_ANSWER_TEXT}\n\n{REVIEW_POLICY_FOLLOWUP}"
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-returns"]
    assert payload["citations"][0]["title"]
    assert payload["citations"][0]["excerpt"]
    assert payload["citations"][0]["kb_version"]
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RETURN_Q_TEXT,
        counter_delta=1,
    )
    assert disk["pending_review_revision"] == 1
    assert disk["summary"] == FULL_SUMMARY
    assert disk["confirmation"] is None


# IT04-06 -------------------------------------------------------------------


async def case_it04_06(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(reject_extraction())
    request_id, response = await send_turn(
        client, session_id, text=NO_TEXT, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=2,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == REJECT_PROMPT
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=NO_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["confirmation"] is None


# IT04-07 -------------------------------------------------------------------


async def case_it04_07(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    assert before["review_rejected"] is True
    calls_before = len(provider.calls)
    provider.script_extraction(change_extraction())
    request_id, response = await send_turn(
        client, session_id, text=CHANGE_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=low_slots,
        summary=LOW_SUMMARY,
        token=3,
    )
    assert payload["reply"] == LOW_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=CHANGE_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"] is None

    # Only a later confirmation completes the recovered intake.
    recovered = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    request_id, confirmed = await post_confirm(
        client, session_id, expected_revision=3, review_revision=3
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    assert_dedicated_confirm_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=recovered,
        payload=confirmed_payload,
        request_id=request_id,
        review_revision=3,
    )


# IT04-08 -------------------------------------------------------------------


async def case_it04_08(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(equal_explicit_correction_extraction())
    text = "Keep the high urgency."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=3,
    )
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text="Keep the high urgency.",
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"] is None


# IT04-09 -------------------------------------------------------------------


async def case_it04_09(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=3,
    )
    assert payload["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["confirmation"] is None


# IT04-10 -------------------------------------------------------------------


async def case_it04_10(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_equal_extraction())
    request_id, response = await send_turn(
        client, session_id, text=YES_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        summary=FULL_SUMMARY,
        token=3,
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=YES_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["slots"] == FULL_SLOTS


# IT04-11 -------------------------------------------------------------------


async def case_it04_11(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(invalid_correction_extraction())
    text = "Change the order to NS-12345."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_issue(payload, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
        pending={
            "order_number": {
                "code": "invalid",
                "candidate": "NS-12345",
                "question": INVALID_ORDER,
            }
        },
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["confirmation"] is None


# IT04-12 -------------------------------------------------------------------


async def case_it04_12(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(valid_correction_plus_invalid_sibling_extraction())
    text = "Make it low urgency, and the order is NS-12345."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_issue(payload, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=low_slots,
        summary=None,
        token=None,
        pending={
            "order_number": {
                "code": "invalid",
                "candidate": "NS-12345",
                "question": INVALID_ORDER,
            }
        },
    )
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is False
    assert disk["pending_review_revision"] is None


# IT04-13 -------------------------------------------------------------------


async def case_it04_13(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(unrelated_extraction())
    text = "It is raining today."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == REJECT_PROMPT
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["confirmation"] is None


# IT04-14 -------------------------------------------------------------------


async def case_it04_14(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(yes_policy_question_extraction())
    provider.script_answer(
        KnowledgeAnswer(
            answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
        )
    )
    request_id, response = await send_turn(
        client, session_id, text=RETURN_Q_TEXT, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=1)
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
    )
    assert payload["reply"] == f"{RETURN_ANSWER_TEXT}\n\n{REJECT_PROMPT}"
    assert [citation["source_id"] for citation in payload["citations"]] == ["faq-returns"]
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=RETURN_Q_TEXT,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["summary"] is None
    assert disk["pending_review_revision"] is None


# IT04-15 -------------------------------------------------------------------


async def case_it04_15(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(reject_with_correction_extraction())
    text = "No, make it low urgency."
    request_id, response = await send_turn(
        client, session_id, text=text, revision=1
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=2,
        status="collecting",
        slots=low_slots,
        summary=None,
        token=None,
    )
    assert payload["reply"] == REJECT_PROMPT
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["slots"] == low_slots
    assert disk["confirmation"] is None


# IT04-16 -------------------------------------------------------------------


async def case_it04_16(client, provider, session_dir):
    session_id = await rejected_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    provider.script_extraction(correction_with_question_extraction())
    text = "Make it low urgency. Can I return an unused item?"
    request_id, response = await send_turn(
        client, session_id, text=text, revision=2
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_ledger(provider, calls_before=calls_before, extracts=1, answers=0)
    low_slots = {**FULL_SLOTS, "urgency": "low"}
    assert_session_state(
        payload,
        revision=3,
        status="collecting",
        slots=low_slots,
        summary=None,
        token=None,
    )
    assert payload["reply"] == f"{REJECT_PROMPT} {POLICY_QUESTION_SUFFIX}"
    disk = assert_turn_commit(
        session_dir=session_dir,
        session_id=session_id,
        before=before,
        response=payload,
        request_id=request_id,
        text=text,
        counter_delta=1,
    )
    assert disk["review_rejected"] is True
    assert disk["slots"] == low_slots


# IT04-17 -------------------------------------------------------------------


async def case_it04_17(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)
    _, response = await post_confirm(
        client, session_id, expected_revision=1, review_revision=0
    )
    await assert_error(
        response,
        status=409,
        code="stale_review",
        message=STALE_REVIEW_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    after = read_session(session_dir, session_id)
    assert after == before
    assert after["status"] == "awaiting_confirmation"
    assert after["pending_review_revision"] == 1
    assert after["summary"] == FULL_SUMMARY
    assert after["confirmation"] is None
    assert len(after["messages"]) == 3
    assert len(after["requests"]) == 1


# IT04-18 -------------------------------------------------------------------


async def case_it04_18(client, provider, session_dir):
    session_id = await start_session(client)
    before = read_session(session_dir, session_id)
    provider.script_extraction(part_extraction())
    turn = await client.post(
        f"/api/sessions/{session_id}/turns",
        json=turn_body("My order NS-000123 arrived broken.", 0),
    )
    assert turn.status_code == 200, turn.text
    incomplete = read_session(session_dir, session_id)
    assert_session_state(
        incomplete,
        revision=1,
        status="collecting",
        slots={**FULL_SLOTS, "category": None, "urgency": None},
        summary=None,
        token=None,
    )
    assert incomplete["pending_clarifications"] == {}
    calls_before = len(provider.calls)
    _, response = await post_confirm(
        client, session_id, expected_revision=1, review_revision=0
    )
    await assert_error(
        response,
        status=409,
        code="review_not_ready",
        message=REVIEW_NOT_READY_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    after = read_session(session_dir, session_id)
    assert after == incomplete
    assert after["pending_review_revision"] is None
    assert after["summary"] is None
    assert after["status"] == "collecting"
    assert after["revision"] == 1
    assert before["revision"] == 0


# IT04-19 -------------------------------------------------------------------


async def case_it04_19(client, provider, session_dir):
    session_id = await review_session(client, provider)
    before = read_session(session_dir, session_id)
    provider.script_extraction(ambiguous_urgency_extraction())
    text = "I am not sure about the urgency."
    turn = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(text, 1)
    )
    assert turn.status_code == 200, turn.text
    blocked = turn.json()
    assert_issue(blocked, "urgency", "ambiguous", None, AMBIGUOUS_URGENCY)
    assert_session_state(
        blocked,
        revision=2,
        status="collecting",
        slots=FULL_SLOTS,
        summary=None,
        token=None,
        pending={
            "urgency": {
                "code": "ambiguous",
                "candidate": None,
                "question": AMBIGUOUS_URGENCY,
            }
        },
    )
    disk_blocked = read_session(session_dir, session_id)
    assert disk_blocked["pending_review_revision"] is None
    assert disk_blocked["slots"] == FULL_SLOTS
    assert disk_blocked["summary"] is None

    calls_before = len(provider.calls)
    _, response = await post_confirm(
        client, session_id, expected_revision=2, review_revision=1
    )
    await assert_error(
        response,
        status=409,
        code="review_not_ready",
        message=REVIEW_NOT_READY_MESSAGE,
    )
    assert_ledger(provider, calls_before=calls_before, extracts=0, answers=0)
    after = read_session(session_dir, session_id)
    assert after == disk_blocked
    assert after["pending_review_revision"] is None
    assert after["slots"] == FULL_SLOTS
    assert after["confirmation"] is None
    assert before["revision"] == 1


CASES = [
    pytest.param(case_it04_01, id="IT04-01"),
    pytest.param(case_it04_02, id="IT04-02"),
    pytest.param(case_it04_03, id="IT04-03"),
    pytest.param(case_it04_04, id="IT04-04"),
    pytest.param(case_it04_05, id="IT04-05"),
    pytest.param(case_it04_06, id="IT04-06"),
    pytest.param(case_it04_07, id="IT04-07"),
    pytest.param(case_it04_08, id="IT04-08"),
    pytest.param(case_it04_09, id="IT04-09"),
    pytest.param(case_it04_10, id="IT04-10"),
    pytest.param(case_it04_11, id="IT04-11"),
    pytest.param(case_it04_12, id="IT04-12"),
    pytest.param(case_it04_13, id="IT04-13"),
    pytest.param(case_it04_14, id="IT04-14"),
    pytest.param(case_it04_15, id="IT04-15"),
    pytest.param(case_it04_16, id="IT04-16"),
    pytest.param(case_it04_17, id="IT04-17"),
    pytest.param(case_it04_18, id="IT04-18"),
    pytest.param(case_it04_19, id="IT04-19"),
]


@pytest.mark.parametrize("case", CASES)
async def test_it04_confirmation_rejection_matrix(case, client, provider, session_dir):
    await case(client, provider, session_dir)
