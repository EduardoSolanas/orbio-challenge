"""IT04 regression: a policy question at a valid review keeps the review current.

Dedicated confirm and a natural-language YES both carry the current revision with
the preserved review token (BS §5.7, IC §13.3 J08). Every case runs against the
real app factory, real lifespan, real engine, the real seed knowledge base and a
real JSON repository in a temporary directory; only the AI service boundary is a
scripted fake (IC §13.1).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.schemas import KnowledgeAnswer
from tests.integration.support_api import (
    ScriptedProvider,
    make_extraction,
    read_session,
)
from tests.integration.test_it02_intake import (
    FULL_SLOTS,
    FULL_SUMMARY,
    category_extraction,
    high_extraction,
    part_extraction,
    post_turn,
    start_session,
    turn_body,
)

PART_TEXT = "My order NS-000123 arrived broken."
CATEGORY_TEXT = "It is a damaged item."
HIGH_TEXT = "High urgency."
RETURN_Q_TEXT = "Can I return an unused item?"
YES_TEXT = "Yes."
RETURN_ANSWER_TEXT = (
    "You can request a return within 30 calendar days of delivery for an unused item in "
    "its original packaging. Refund eligibility requires support review."
)
REVIEW_SUFFIX_TEXT = "Please confirm these details or tell me what to change."
CONFIRM_USER_TEXT = "Confirm reviewed intake"
STALE_REVIEW_MESSAGE = "The review changed. Review the latest details before confirming."
RETURN_CITATION = {
    "source_id": "faq-returns",
    "title": "Returns and refunds",
    "excerpt": (
        "An unused item in its original packaging can be requested for return within 30 "
        "calendar days of delivery. Refund eligibility requires a support review; this "
        "demo cannot approve or issue a refund."
    ),
    "kb_version": "2026-10-22",
}


def receipt_for(session_id: str) -> str:
    return f"Saved locally as {session_id}. No request has been sent to a real support team."


def question_extraction():
    return make_extraction(intent="question", has_question=True)


def yes_extraction():
    return make_extraction(intent="confirm")


def return_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True, answer=RETURN_ANSWER_TEXT, cited_ids=["faq-returns"]
    )


async def review_session(client, provider: ScriptedProvider) -> str:
    """PART → CATEGORY → HIGH: a structured review at revision 3, token 3."""

    session_id = await start_session(client)
    await post_turn(
        client,
        provider,
        session_id,
        text=PART_TEXT,
        revision=0,
        extraction=part_extraction(),
    )
    await post_turn(
        client,
        provider,
        session_id,
        text=CATEGORY_TEXT,
        revision=1,
        extraction=category_extraction(),
    )
    _, payload = await post_turn(
        client,
        provider,
        session_id,
        text=HIGH_TEXT,
        revision=2,
        extraction=high_extraction(),
    )
    assert payload["revision"] == 3
    assert payload["status"] == "awaiting_confirmation"
    assert payload["pending_review_revision"] == 3
    assert payload["summary"] == FULL_SUMMARY
    assert payload["reply"] == FULL_SUMMARY + "\n" + REVIEW_SUFFIX_TEXT
    return session_id


async def ask_at_review(client, provider: ScriptedProvider, session_id: str) -> dict:
    """A pure policy question at the review: revision 4, token still 3."""

    provider.script_extraction(question_extraction())
    provider.script_answer(return_answer())
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(RETURN_Q_TEXT, 3)
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["revision"] == 4
    assert payload["status"] == "awaiting_confirmation"
    assert payload["pending_review_revision"] == 3
    assert payload["summary"] == FULL_SUMMARY
    assert payload["slots"] == FULL_SLOTS
    assert payload["pending_clarifications"] == {}
    assert payload["reply"] == RETURN_ANSWER_TEXT
    assert payload["citations"] == [RETURN_CITATION]
    return payload


async def post_confirm(
    client,
    session_id: str,
    *,
    expected_revision: int,
    review_revision: int,
) -> tuple[str, object]:
    request_id = str(uuid4())
    response = await client.post(
        f"/api/sessions/{session_id}/confirm",
        json={
            "request_id": request_id,
            "expected_revision": expected_revision,
            "review_revision": review_revision,
        },
    )
    return request_id, response


async def test_it04_question_at_review_then_dedicated_confirm_completes(
    client, provider: ScriptedProvider, session_dir: Path
):
    session_id = await review_session(client, provider)
    await ask_at_review(client, provider, session_id)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)

    request_id, response = await post_confirm(
        client, session_id, expected_revision=4, review_revision=3
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    receipt = receipt_for(session_id)
    assert payload["status"] == "completed"
    assert payload["revision"] == 5
    assert payload["reply"] == receipt
    assert payload["slots"] == FULL_SLOTS
    assert payload["summary"] == FULL_SUMMARY
    assert payload["pending_clarifications"] == {}
    assert payload["pending_review_revision"] is None
    assert payload["citations"] == []
    assert len(provider.calls) == calls_before

    after = read_session(session_dir, session_id)
    assert after["status"] == "completed"
    assert after["revision"] == 5
    assert after["collection_turn_count"] == 4
    assert after["pending_review_revision"] is None
    assert after["summary"] == FULL_SUMMARY
    assert after["confirmation"]["review_revision"] == 3
    assert after["confirmation"]["confirmed_at"].endswith("Z")
    assert after["messages"][:-2] == before["messages"]
    assert len(after["messages"]) == len(before["messages"]) + 2
    user, assistant = after["messages"][-2:]
    assert user["role"] == "user"
    assert user["text"] == CONFIRM_USER_TEXT
    assert user["input_mode"] == "system"
    assert assistant["role"] == "assistant"
    assert assistant["text"] == receipt
    assert assistant["id"] == payload["message_id"]
    assert after["requests"][:-1] == before["requests"]
    assert len(after["requests"]) == len(before["requests"]) + 1
    assert after["requests"][-1]["request_id"] == request_id
    assert after["requests"][-1]["response"] == payload
    assert not provider.extractions and not provider.answers


async def test_it04_question_at_review_then_yes_completes(
    client, provider: ScriptedProvider, session_dir: Path
):
    session_id = await review_session(client, provider)
    await ask_at_review(client, provider, session_id)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)

    provider.script_extraction(yes_extraction())
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(YES_TEXT, 4)
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    receipt = receipt_for(session_id)
    assert payload["status"] == "completed"
    assert payload["revision"] == 5
    assert payload["reply"] == receipt
    assert payload["slots"] == FULL_SLOTS
    assert payload["summary"] == FULL_SUMMARY
    assert payload["pending_clarifications"] == {}
    assert payload["pending_review_revision"] is None
    assert payload["citations"] == []
    assert provider.methods()[calls_before:] == ["extract"]

    after = read_session(session_dir, session_id)
    assert after["status"] == "completed"
    assert after["revision"] == 5
    assert after["collection_turn_count"] == 4
    assert after["confirmation"]["review_revision"] == 3
    assert after["messages"][:-2] == before["messages"]
    assert len(after["messages"]) == len(before["messages"]) + 2
    assert after["messages"][-2]["text"] == YES_TEXT
    assert after["messages"][-2]["input_mode"] == "text"
    assert after["messages"][-1]["text"] == receipt
    assert after["messages"][-1]["id"] == payload["message_id"]
    assert after["requests"][-1]["response"] == payload
    assert not provider.extractions and not provider.answers


async def test_it04_question_at_review_then_a_stale_token_is_stale_review(
    client, provider: ScriptedProvider, session_dir: Path
):
    session_id = await review_session(client, provider)
    await ask_at_review(client, provider, session_id)
    before = read_session(session_dir, session_id)
    calls_before = len(provider.calls)

    _, response = await post_confirm(
        client, session_id, expected_revision=4, review_revision=2
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "stale_review"
    assert body["error"]["message"] == STALE_REVIEW_MESSAGE
    assert body["error"]["retryable"] is False
    assert len(provider.calls) == calls_before
    after = read_session(session_dir, session_id)
    assert after == before
    assert after["status"] == "awaiting_confirmation"
    assert after["revision"] == 4
    assert after["pending_review_revision"] == 3
    assert after["confirmation"] is None
