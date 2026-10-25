"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): speech deadline."""

from __future__ import annotations

from uuid import uuid4

import pytest

from gateway.errors import AppError
from gateway.providers.prompts import GREETING, NOTHING_TO_CONFIRM
from gateway.schemas import KnowledgeAnswer
from tests.support.unit_fakes import SlowProvider, make_extraction, make_record
from tests.unit.gateway.conversation.conversation_support import (
    FULL_EXTRACTION,
    build_engine,
    turn_request,
)


async def test_assistant_text_returns_the_saved_assistant_message(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    response = await engine.turn(created.session_id, turn_request())

    assert await engine.assistant_text(
        created.session_id, created.messages[0].id
    ) == GREETING
    assert await engine.assistant_text(
        created.session_id, response.message_id
    ) == response.reply
    provider.assert_exhausted()


async def test_assistant_text_rejects_user_messages_and_unknown_ids(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    await engine.turn(created.session_id, turn_request())
    stored = await repository.load(created.session_id)
    user_message = stored.messages[1]

    with pytest.raises(AppError) as user_error:
        await engine.assistant_text(created.session_id, user_message.id)
    assert user_error.value.code == "message_not_found"

    with pytest.raises(AppError) as unknown_error:
        await engine.assistant_text(created.session_id, uuid4())
    assert unknown_error.value.code == "message_not_found"
    provider.assert_exhausted()


async def test_assistant_text_missing_session_raises_session_not_found(tmp_path):
    engine, _, _ = build_engine(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await engine.assistant_text(uuid4(), uuid4())

    assert excinfo.value.code == "session_not_found"


async def test_extraction_deadline_maps_provider_timeout_and_saves_nothing(
    tmp_path, monkeypatch
):
    import gateway.conversation.engine as conversation_module

    monkeypatch.setattr(conversation_module, "TEXT_TURN_DEADLINE_SECONDS", 0.05)
    engine, repository, provider = build_engine(tmp_path, script=[])
    provider = SlowProvider(
        [("extract", FULL_EXTRACTION)], delay=0.2
    )
    engine.provider = provider
    created = await engine.create_session()
    sid = created.session_id

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request())

    assert excinfo.value.code == "provider_timeout"
    assert excinfo.value.status == 504
    stored = await repository.load(sid)
    assert stored.revision == 0
    assert len(stored.messages) == 1
    assert stored.requests == []


async def test_rag_answer_shares_the_extraction_deadline(tmp_path, monkeypatch):
    import gateway.conversation.engine as conversation_module

    monkeypatch.setattr(conversation_module, "TEXT_TURN_DEADLINE_SECONDS", 0.05)
    record = make_record()
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", make_extraction(intent="question", has_question=True))],
        kb_answers={"Can I return an unused item?": [record]},
    )
    engine.provider = SlowProvider(
        [
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True,
                    answer="Within 30 calendar days.",
                    cited_ids=["faq-returns"],
                ),
            )
        ],
        delay=0.2,
    )
    created = await engine.create_session()
    sid = created.session_id

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request("Can I return an unused item?"))

    assert excinfo.value.code == "provider_timeout"
    stored = await repository.load(sid)
    assert stored.revision == 0
    assert len(stored.messages) == 1
    assert stored.requests == []


async def test_natural_confirmation_without_a_current_review_does_not_complete(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", make_extraction(intent="confirm"))],
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Yes, that is right")
    )

    assert response.revision == 1
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == NOTHING_TO_CONFIRM
    provider.assert_exhausted()
