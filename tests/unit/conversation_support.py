"""Shared helpers for the split test_conversation modules."""

from __future__ import annotations

from uuid import uuid4

from app.conversation import ConversationEngine
from app.schemas import ConfirmRequest, TurnRequest
from app.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_slots,
)


FULL_SLOTS = make_slots(
    order_number="NS-000123",
    category="damaged_item",
    description="My order arrived broken",
    urgency="high",
)


FULL_EXTRACTION = make_extraction(
    candidates={
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": "high",
    }
)


SUMMARY = (
    "Account reference: NS-000123\n"
    "Energy support area: equipment issue\n"
    "Issue: My order arrived broken\n"
    "Urgency: high"
)


def build_engine(tmp_path, script=(), kb_answers=None):
    repository = JsonSessionRepository(tmp_path)
    provider = FakeProvider(script)
    knowledge_base = FakeKnowledgeBase(kb_answers)
    return ConversationEngine(repository, provider, knowledge_base), repository, provider


def turn_request(text="NS-000123 arrived broken and this is urgent", revision=0, request_id=None):
    return TurnRequest(
        request_id=request_id or uuid4(),
        expected_revision=revision,
        text=text,
        input_mode="text",
    )


async def assert_state(
    repository,
    session_id,
    response,
    *,
    revision,
    status,
    rejected,
    token,
    summary,
):
    stored = await repository.load(session_id)
    for label, value in (
        ("response.revision", response.revision),
        ("stored.revision", stored.revision),
    ):
        assert value == revision, label
    assert response.status == status
    assert stored.status == status
    assert response.pending_review_revision == token
    assert stored.pending_review_revision == token
    assert response.summary == summary
    assert stored.summary == summary
    assert stored.review_rejected is rejected


def rejection() -> object:
    return make_extraction(intent="reject")


def confirm_equal(urgency: str = "high") -> object:
    return make_extraction(
        intent="confirm",
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": urgency,
        },
    )


def confirm_request(revision=1, review_revision=1, request_id=None):
    return ConfirmRequest(
        request_id=request_id or uuid4(),
        expected_revision=revision,
        review_revision=review_revision,
    )
