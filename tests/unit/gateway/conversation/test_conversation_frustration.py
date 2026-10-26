"""Frustration classification and simulated agent handoff."""
from __future__ import annotations

from uuid import uuid4

import pytest

from gateway.conversation.engine import ConversationEngine, transition_frustration
from gateway.errors import AppError, app_error
from gateway.localization import text
from gateway.schemas import TurnRequest
from gateway.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_session,
    make_slots,
)

TRANSITIONS = [
    (False, "none", False, False),
    (False, "uncertain", False, False),
    (False, "present", True, True),
    (True, "none", False, False),
    (True, "uncertain", True, False),
    (True, "present", True, False),
]
FULL_SLOTS = make_slots(
    order_number="NS-000123", category="damaged_item",
    description="My order arrived broken", urgency="high",
)


def command(message: str, revision: int = 0, request_id=None) -> TurnRequest:
    return TurnRequest(
        request_id=request_id or uuid4(), expected_revision=revision,
        text=message, input_mode="text",
    )


@pytest.mark.parametrize("previous,label,active,handoff", TRANSITIONS)
def test_transition_table(previous, label, active, handoff):
    assert transition_frustration(previous, label) == (active, handoff)


@pytest.mark.asyncio
async def test_new_frustration_hands_off_before_faq_or_issue_processing(tmp_path):
    provider = FakeProvider([("extract", make_extraction(
        intent="question", has_question=True, candidates={"order_number": "NS-000999"},
        sentiment="negative", frustration="present",
    ))])
    knowledge = FakeKnowledgeBase({})
    repository = JsonSessionRepository(tmp_path)
    engine = ConversationEngine(repository, provider, knowledge)
    session = await engine.create_session()

    response = await engine.turn(
        session.session_id, command("I am frustrated; what does a standing charge mean?"),
    )
    stored = await repository.load(session.session_id)

    assert response.status == stored.status == "handed_off"
    assert response.reply == text("frustration_handoff", "en")
    assert response.frustration_active is stored.frustration_active is True
    assert response.slots.model_dump() == session.slots.model_dump()
    assert response.citations == []
    assert stored.messages[-2].frustration == "present"
    assert knowledge.questions == []
    assert [name for name, _ in provider.calls] == ["extract"]


@pytest.mark.asyncio
async def test_negative_or_uncertain_without_present_frustration_does_not_handoff(tmp_path):
    provider = FakeProvider([
        ("extract", make_extraction(candidates={"order_number": "NS-000123"}, sentiment="negative", frustration="none")),
        ("extract", make_extraction(candidates={"description": "The item arrived late"}, sentiment="uncertain", frustration="uncertain")),
    ])
    engine = ConversationEngine(JsonSessionRepository(tmp_path), provider, FakeKnowledgeBase({}))
    session = await engine.create_session()

    first = await engine.turn(session.session_id, command("My item arrived late."))
    second = await engine.turn(session.session_id, command("I am not sure what to say.", 1))

    assert first.status == second.status == "collecting"
    assert first.frustration_active is second.frustration_active is False
    provider.assert_exhausted()


@pytest.mark.asyncio
async def test_handoff_is_localized_and_preserves_review_without_confirming(tmp_path):
    session = make_session(
        status="awaiting_confirmation", slots=FULL_SLOTS,
        pending_review_revision=1, summary="Existing reviewed summary",
    )
    extraction = make_extraction(
        detected_language="es", intent="confirm", sentiment="negative", frustration="present"
    )
    provider = FakeProvider([("extract", extraction)])
    repository = JsonSessionRepository(tmp_path)
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase({}))
    await repository.save(session)

    response = await engine.turn(
        session.session_id, command("Sí, pero estoy muy frustrado.")
    )
    stored = await repository.load(session.session_id)

    assert response.status == "handed_off"
    assert response.reply_language == "es"
    assert response.reply == text("frustration_handoff", "es")
    assert response.summary == "Existing reviewed summary"
    assert response.pending_review_revision == 1
    assert stored.confirmation is None and stored.status == "handed_off"
    provider.assert_exhausted()


@pytest.mark.asyncio
async def test_handed_off_state_is_terminal_but_exact_turn_replay_is_safe(tmp_path):
    provider = FakeProvider([("extract", make_extraction(sentiment="negative", frustration="present"))])
    repository = JsonSessionRepository(tmp_path)
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase({}))
    session = await engine.create_session()
    first_command = command("I am frustrated.")

    first = await engine.turn(session.session_id, first_command)
    replay = await engine.turn(session.session_id, first_command)
    with pytest.raises(AppError) as raised:
        await engine.turn(session.session_id, command("Please continue.", revision=1))

    assert replay == first
    assert raised.value.code == "session_handed_off"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_extraction_failure_does_not_commit_a_handoff(tmp_path):
    class FailingProvider(FakeProvider):
        async def extract(self, context):
            self.calls.append(("extract", context))
            raise app_error("invalid_provider_response")

    repository = JsonSessionRepository(tmp_path)
    engine = ConversationEngine(repository, FailingProvider(), FakeKnowledgeBase({}))
    session = await engine.create_session()
    original = (tmp_path / f"{session.session_id}.json").read_bytes()

    with pytest.raises(AppError):
        await engine.turn(session.session_id, command("I am frustrated."))

    stored = await repository.load(session.session_id)
    assert (tmp_path / f"{session.session_id}.json").read_bytes() == original
    assert stored.status == "collecting" and stored.frustration_active is False


@pytest.mark.asyncio
async def test_atomic_save_failure_rolls_back_handoff_state(tmp_path, monkeypatch):
    import gateway.storage as storage

    provider = FakeProvider([("extract", make_extraction(sentiment="negative", frustration="present"))])
    repository = JsonSessionRepository(tmp_path)
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase({}))
    session = await engine.create_session()
    path = tmp_path / f"{session.session_id}.json"
    original = path.read_bytes()
    monkeypatch.setattr(storage.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(AppError) as raised:
        await engine.turn(session.session_id, command("I am frustrated."))

    assert raised.value.code == "storage_unavailable"
    assert path.read_bytes() == original
    assert (await repository.load(session.session_id)).status == "collecting"
    provider.assert_exhausted()
