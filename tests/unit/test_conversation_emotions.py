"""Unit tests: emotion metadata through the conversation engine (ORB-40).

The engine writes extraction labels on the committed customer message, null
labels on assistant and system messages, and a strict ``frustration_active``
flag in extraction context, stored sessions, views and turn responses.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from app.conversation import ConversationEngine
from app.prompts import GREETING, MISSING_URGENCY
from app.schemas import ConfirmRequest, TurnRequest
from app.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_session,
    make_slots,
)

FULL_SLOTS = make_slots(
    order_number="NS-000123",
    category="damaged_item",
    description="My order arrived broken",
    urgency="high",
)
LEGACY_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "sessions" / "v1-collecting.json"
)


def build_engine(tmp_path, script=()):
    repository = JsonSessionRepository(tmp_path)
    provider = FakeProvider(script)
    return ConversationEngine(repository, provider, FakeKnowledgeBase({})), repository, provider


def turn_request(text="My order NS-000123 arrived broken and I am frustrated.", revision=0):
    return TurnRequest(
        request_id=uuid4(),
        expected_revision=revision,
        text=text,
        input_mode="text",
    )


async def test_new_session_starts_with_a_false_frustration_flag(tmp_path):
    engine, repository, _ = build_engine(tmp_path)

    view = await engine.create_session()

    assert view.frustration_active is False
    assert view.messages[0].sentiment is None
    assert view.messages[0].frustration is None
    stored = await repository.load(view.session_id)
    assert stored.schema_version == 2
    assert stored.frustration_active is False
    assert stored.messages[0].sentiment is None
    assert stored.messages[0].frustration is None


async def test_turn_persists_customer_labels_and_null_assistant_labels(tmp_path):
    extraction = make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": "high",
        },
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(tmp_path, script=[("extract", extraction)])
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request())

    stored = await repository.load(created.session_id)
    assert stored.schema_version == 2
    user_message, assistant_message = stored.messages[-2:]
    assert user_message.role == "user"
    assert user_message.sentiment == "negative"
    assert user_message.frustration == "present"
    assert assistant_message.role == "assistant"
    assert assistant_message.sentiment is None
    assert assistant_message.frustration is None
    assert response.frustration_active == stored.frustration_active
    provider.assert_exhausted()


async def test_transcript_turns_persist_customer_labels_too(tmp_path):
    extraction = make_extraction(
        candidates={"description": "My order arrived broken"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(tmp_path, script=[("extract", extraction)])
    created = await engine.create_session()

    await engine.turn(
        created.session_id,
        TurnRequest(
            request_id=uuid4(),
            expected_revision=0,
            text="My order arrived broken and I am frustrated.",
            input_mode="transcript",
        ),
    )

    stored = await repository.load(created.session_id)
    assert stored.messages[-2].input_mode == "transcript"
    assert stored.messages[-2].sentiment == "negative"
    assert stored.messages[-2].frustration == "present"
    provider.assert_exhausted()


async def test_extraction_context_carries_the_committed_flag_and_recent_history(tmp_path):
    extraction = make_extraction(sentiment="neutral", frustration="none")
    engine, repository, provider = build_engine(tmp_path, script=[("extract", extraction)])
    created = await engine.create_session()

    await engine.turn(created.session_id, turn_request("Hello there.", revision=0))

    context = provider.calls[0][1]
    assert context.frustration_active is False
    assert context.current_message == "Hello there."
    assert [(message.role, message.text) for message in context.recent_messages] == [
        ("assistant", GREETING)
    ]
    stored = await repository.load(created.session_id)
    assert stored.messages[-2].sentiment == "neutral"
    assert stored.messages[-2].frustration == "none"
    provider.assert_exhausted()


async def test_confirmation_messages_use_null_labels_and_preserve_the_flag(tmp_path):
    session = make_session(
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        pending_review_revision=1,
        summary="Order: NS-000123",
        frustration_active=True,
    )
    engine, repository, provider = build_engine(tmp_path)
    await repository.save(session)

    response = await engine.confirm(
        session.session_id,
        ConfirmRequest(
            request_id=uuid4(), expected_revision=0, review_revision=1
        ),
    )

    stored = await repository.load(session.session_id)
    assert response.frustration_active is True
    assert stored.frustration_active is True
    assert stored.messages[-2].input_mode == "system"
    assert stored.messages[-2].sentiment is None
    assert stored.messages[-2].frustration is None
    assert stored.messages[-1].sentiment is None
    assert stored.messages[-1].frustration is None
    provider.assert_exhausted()


async def test_legacy_session_labels_stay_null_and_the_next_turn_writes_version_two(
    tmp_path,
):
    document = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))
    path = tmp_path / f"{document['session_id']}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    extraction = make_extraction(
        candidates={"description": "My order arrived broken"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(tmp_path, script=[("extract", extraction)])

    response = await engine.turn(
        UUID(document["session_id"]),
        TurnRequest(
            request_id=uuid4(),
            expected_revision=0,
            text="My order arrived broken and I am frustrated.",
            input_mode="text",
        ),
    )

    stored = await repository.load(UUID(document["session_id"]))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert response.revision == 1
    assert saved["schema_version"] == 2
    assert saved["frustration_active"] is False
    assert stored.messages[0].sentiment is None
    assert stored.messages[0].frustration is None
    assert stored.messages[-2].sentiment == "negative"
    assert stored.messages[-2].frustration == "present"
    assert stored.messages[-1].sentiment is None
    provider.assert_exhausted()


async def test_frustration_present_never_supplies_an_urgency_value(tmp_path):
    extraction = make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My item is broken.",
        },
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(tmp_path, script=[("extract", extraction)])
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("I have asked three times. This is really frustrating.", revision=0),
    )

    stored = await repository.load(created.session_id)
    assert response.slots.urgency is None
    assert stored.slots.urgency is None
    assert response.reply == MISSING_URGENCY
    assert stored.messages[-2].frustration == "present"
    provider.assert_exhausted()


async def test_second_turn_classifies_with_the_committed_conversation_context(tmp_path):
    first = make_extraction(
        candidates={"description": "My item is broken."},
        sentiment="negative",
        frustration="none",
    )
    second = make_extraction(
        intent="unrelated", sentiment="negative", frustration="present"
    )
    engine, _, provider = build_engine(
        tmp_path, script=[("extract", first), ("extract", second)]
    )
    created = await engine.create_session()

    first_response = await engine.turn(
        created.session_id, turn_request("My item is broken.", revision=0)
    )
    await engine.turn(
        created.session_id,
        turn_request("I have asked three times. This is really frustrating.", revision=1),
    )

    context = provider.calls[1][1]
    assert [message.text for message in context.recent_messages] == [
        GREETING,
        "My item is broken.",
        first_response.reply,
    ]
    assert (
        context.current_message
        == "I have asked three times. This is really frustrating."
    )
    assert context.frustration_active is False
    provider.assert_exhausted()
