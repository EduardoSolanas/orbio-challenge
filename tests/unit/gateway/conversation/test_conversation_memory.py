"""authoritative extraction memory beyond the 12-message window.

Unit coverage for the exact ExtractionContext built from a committed session and
for the detached copies handed to the provider. Only the AI service boundary and
the retrieval boundary are doubled (IC §13.1); the engine, merge rules and JSON
repository are the real implementations.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from gateway.conversation.engine import (
    RECENT_MESSAGE_LIMIT,
    ConversationEngine,
    build_extraction_context,
)
from gateway.errors import AppError, app_error
from gateway.schemas import (
    Clarification,
    KnowledgeAnswer,
    RecentMessage,
    TurnRequest,
)
from gateway.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_message,
    make_record,
    make_session,
    make_slots,
)

HISTORY_SIZES = (0, 1, 11, 12, 13, 25)
RETURN_QUESTION = "Can I return an unused item?"
RETURN_BODY = "An unused item can be requested for return within 30 calendar days."

PENDING_CATEGORY = Clarification(
    code="ambiguous",
    candidate="other",
    question="Please clarify your energy support area.",
)


def history(size: int) -> list:
    return [
        make_message(text=f"message {index}", role="user" if index % 2 else "assistant")
        for index in range(size)
    ]


def question_extraction():
    return make_extraction(intent="question", has_question=True)


def grounded_answer() -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answerable=True,
        answer=RETURN_BODY,
        cited_ids=["faq-returns"],
    )


def turn_request(text: str, revision: int) -> TurnRequest:
    return TurnRequest(
        request_id=uuid4(),
        expected_revision=revision,
        text=text,
        input_mode="text",
    )


# --------------------------------------------------------------------------- #
# Exact window (acceptance: history sizes 0, 1, 11, 12, 13 and 25)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", HISTORY_SIZES)
def test_context_takes_exactly_the_latest_twelve_messages_in_stored_order(size):
    messages = history(size)
    session = make_session(messages=messages)

    context = build_extraction_context(session, "current message")

    assert len(context.recent_messages) == min(size, RECENT_MESSAGE_LIMIT)
    assert context.recent_messages == [
        RecentMessage(role=message.role, text=message.text)
        for message in messages[-RECENT_MESSAGE_LIMIT:]
    ]
    assert context.current_message == "current message"
    assert "current message" not in [item.text for item in context.recent_messages]


def test_context_carries_the_authoritative_session_state():
    session = make_session(
        revision=4,
        status="awaiting_confirmation",
        reply_language="es",
        frustration_active=True,
        slots=make_slots(order_number="NS-000123"),
        messages=history(13),
        pending_clarifications={"category": PENDING_CATEGORY},
        pending_review_revision=3,
        summary="Account reference: NS-000123",
    )

    context = build_extraction_context(session, "hola")

    assert context.slots.model_dump() == {
        "order_number": "NS-000123",
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert context.pending_clarifications == {"category": PENDING_CATEGORY}
    assert context.status == "awaiting_confirmation"
    assert context.reply_language == "es"
    assert context.frustration_active is True
    assert context.intake_started is True


def test_context_marks_a_fresh_session_as_not_started():
    session = make_session(messages=history(1))

    context = build_extraction_context(session, "hello")

    assert context.intake_started is False


@pytest.mark.parametrize(
    "state",
    [
        {"slots": make_slots(order_number="NS-000123")},
        {"pending_clarifications": {"category": PENDING_CATEGORY}},
        {"review_rejected": True},
        {"pending_review_revision": 2},
        {"summary": "Account reference: NS-000123"},
        {"status": "completed"},
    ],
    ids=[
        "accepted-slot",
        "pending-clarification",
        "rejected-review",
        "review-token",
        "summary",
        "completed",
    ],
)
def test_context_intake_started_is_true_for_every_committed_intake_marker(state):
    session = make_session(messages=history(1), **state)

    context = build_extraction_context(session, "hello")

    assert context.intake_started is True


# --------------------------------------------------------------------------- #
# Detached context objects
# --------------------------------------------------------------------------- #


def test_mutating_the_context_cannot_change_the_session():
    session = make_session(
        slots=make_slots(order_number="NS-000123"),
        messages=history(13),
        pending_clarifications={"category": PENDING_CATEGORY},
    )

    context = build_extraction_context(session, "current")

    context.slots.order_number = "NS-999999"
    context.pending_clarifications["category"].question = "mutated"
    context.pending_clarifications["urgency"] = Clarification(
        code="invalid", candidate=None, question="injected"
    )
    context.recent_messages.append(RecentMessage(role="user", text="injected"))
    context.recent_messages[0].text = "rewritten"
    context.status = "completed"
    context.frustration_active = True
    context.intake_started = False

    assert session.slots.order_number == "NS-000123"
    assert session.pending_clarifications == {"category": PENDING_CATEGORY}
    assert len(session.messages) == 13
    assert session.messages[0].text == "message 0"
    assert session.status == "collecting"
    assert session.frustration_active is False


class MutatingExtractionProvider(FakeProvider):
    """Provider that tries to corrupt every authoritative field it is handed."""

    async def extract(self, context):
        context.slots.order_number = "NS-999999"
        context.slots.urgency = "high"
        context.pending_clarifications.clear()
        context.recent_messages.append(RecentMessage(role="user", text="injected"))
        context.recent_messages[0].text = "rewritten"
        context.status = "completed"
        context.frustration_active = True
        context.intake_started = False
        context.reply_language = "es"
        return await super().extract(context)


async def test_provider_mutation_of_the_context_cannot_alter_stored_state(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    session_id = uuid4()
    messages = history(25)
    pending = {"category": PENDING_CATEGORY}
    await repository.save(
        make_session(
            session_id=session_id,
            revision=0,
            slots=make_slots(order_number="NS-000123"),
            messages=messages,
            pending_clarifications=pending,
        )
    )
    provider = MutatingExtractionProvider(
        [
            ("extract", question_extraction()),
            ("answer_knowledge", grounded_answer()),
        ]
    )
    engine = ConversationEngine(
        repository,
        provider,
        FakeKnowledgeBase({RETURN_QUESTION: [make_record()]}),
    )

    response = await engine.turn(session_id, turn_request(RETURN_QUESTION, 0))

    stored = await repository.load(session_id)
    assert response.slots.order_number == "NS-000123"
    assert stored.slots.order_number == "NS-000123"
    assert stored.slots.urgency is None
    assert stored.pending_clarifications == pending
    assert stored.status == "collecting"
    assert stored.frustration_active is False
    assert stored.reply_language == "en"
    assert len(stored.messages) == 27
    assert stored.messages[0].text == "message 0"
    assert [message.text for message in stored.messages[-2:]] == [
        RETURN_QUESTION,
        response.reply,
    ]


# --------------------------------------------------------------------------- #
# Corrections only touch the intended field
# --------------------------------------------------------------------------- #


async def test_urgency_correction_keeps_an_order_number_outside_the_window(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    session_id = uuid4()
    messages = history(25)
    await repository.save(
        make_session(
            session_id=session_id,
            revision=0,
            slots=make_slots(
                order_number="NS-000123",
                category="damaged_item",
                description="My order arrived broken",
            ),
            messages=messages,
        )
    )
    provider = FakeProvider(
        [
            (
                "extract",
                make_extraction(
                    intent="correction",
                    candidates={"urgency": "high"},
                    explicit_corrections=["urgency"],
                ),
            )
        ]
    )
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    response = await engine.turn(session_id, turn_request("Make the urgency high.", 0))

    method, context = provider.calls[0]
    assert method == "extract"
    assert context.slots.order_number == "NS-000123"
    assert "NS-000123" not in [message.text for message in context.recent_messages]
    assert len(context.recent_messages) == 12
    assert response.slots.model_dump() == {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": "high",
    }
    stored = await repository.load(session_id)
    assert stored.slots == response.slots
    assert len(stored.messages) == 27


# --------------------------------------------------------------------------- #
# Pending ambiguity survives policy detours
# --------------------------------------------------------------------------- #


async def test_pending_ambiguity_survives_a_faq_detour(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    session_id = uuid4()
    await repository.save(
        make_session(
            session_id=session_id,
            revision=0,
            slots=make_slots(order_number="NS-000123"),
            messages=history(13),
            pending_clarifications={"category": PENDING_CATEGORY},
        )
    )
    provider = FakeProvider(
        [
            ("extract", question_extraction()),
            ("answer_knowledge", grounded_answer()),
            ("extract", make_extraction(candidates={"category": "delivery"})),
        ]
    )
    engine = ConversationEngine(
        repository,
        provider,
        FakeKnowledgeBase({RETURN_QUESTION: [make_record()]}),
    )

    detour = await engine.turn(session_id, turn_request(RETURN_QUESTION, 0))

    assert detour.reply == RETURN_BODY
    assert detour.slots.order_number == "NS-000123"
    assert detour.pending_clarifications == {"category": PENDING_CATEGORY}
    assert detour.status == "collecting"
    stored = await repository.load(session_id)
    assert stored.pending_clarifications == {"category": PENDING_CATEGORY}
    _, context = provider.calls[0]
    assert context.pending_clarifications == {"category": PENDING_CATEGORY}
    assert context.intake_started is True

    resolved = await engine.turn(
        session_id, turn_request("It is a delivery problem.", 1)
    )

    assert resolved.pending_clarifications == {}
    assert resolved.slots.order_number == "NS-000123"
    assert resolved.slots.category == "delivery"
    stored = await repository.load(session_id)
    assert stored.pending_clarifications == {}


# --------------------------------------------------------------------------- #
# Failed turns never enter the window
# --------------------------------------------------------------------------- #


class FailingExtractionProvider(FakeProvider):
    async def extract(self, context):
        if not self._script:
            self.calls.append(("extract", context))
            raise app_error("invalid_provider_response")
        return await super().extract(context)


async def test_failed_turn_is_excluded_from_the_next_context(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    session_id = uuid4()
    messages = history(13)
    await repository.save(
        make_session(session_id=session_id, revision=0, messages=messages)
    )
    provider = FailingExtractionProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.turn(session_id, turn_request("unsent draft", 0))

    assert excinfo.value.code == "invalid_provider_response"
    stored = await repository.load(session_id)
    assert len(stored.messages) == 13

    provider._script.append(("extract", make_extraction(intent="social")))
    await engine.turn(session_id, turn_request("Hello", 0))
    _, context = provider.calls[-1]
    assert [message.text for message in context.recent_messages] == [
        message.text for message in messages[-12:]
    ]
    assert "unsent draft" not in [message.text for message in context.recent_messages]


# --------------------------------------------------------------------------- #
# RAG context carries only its own question, sources and language
# --------------------------------------------------------------------------- #


async def test_knowledge_context_has_no_slots_or_session_history(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    session_id = uuid4()
    record = make_record()
    await repository.save(
        make_session(
            session_id=session_id,
            revision=0,
            reply_language="es",
            slots=make_slots(order_number="NS-000123"),
            messages=history(13),
        )
    )
    provider = FakeProvider(
        [
            (
                "extract",
                make_extraction(
                    intent="question", has_question=True, detected_language="es"
                ),
            ),
            ("answer_knowledge", grounded_answer()),
        ]
    )
    knowledge_base = FakeKnowledgeBase({RETURN_QUESTION: [record]})
    engine = ConversationEngine(repository, provider, knowledge_base)

    await engine.turn(session_id, turn_request(RETURN_QUESTION, 0))

    method, context = provider.calls[-1]
    assert method == "answer_knowledge"
    assert context.question == RETURN_QUESTION
    assert context.sources == [record]
    assert context.reply_language == "es"
    assert context.kb_version == knowledge_base.version
    assert set(context.model_dump()) == {
        "question",
        "kb_version",
        "sources",
        "reply_language",
    }
