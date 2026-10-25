"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): turns."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.gateway.errors import AppError
from backend.gateway.providers.prompts import GREETING
from tests.support.unit_fakes import make_slots
from tests.unit.gateway.conversation.conversation_support import (
    FULL_EXTRACTION,
    FULL_SLOTS,
    SUMMARY,
    build_engine,
    turn_request,
)


async def test_create_session_persists_the_greeting_at_revision_zero(tmp_path):
    engine, repository, provider = build_engine(tmp_path)

    view = await engine.create_session()

    assert view.revision == 0
    assert view.status == "collecting"
    assert view.summary is None
    assert view.pending_review_revision is None
    assert view.slots == make_slots()
    assert len(view.messages) == 1
    message = view.messages[0]
    assert message.role == "assistant"
    assert message.text == GREETING
    assert message.input_mode == "system"
    assert message.citations == []

    stored = await repository.load(view.session_id)
    assert stored.revision == 0
    assert stored.collection_turn_count == 0
    assert stored.review_rejected is False
    assert stored.requests == []
    assert len(stored.messages) == 1
    provider.assert_exhausted()


async def test_get_session_returns_the_persisted_view(tmp_path):
    engine, _, _ = build_engine(tmp_path)
    created = await engine.create_session()

    fetched = await engine.get_session(created.session_id)

    assert fetched == created


async def test_get_session_missing_raises_session_not_found(tmp_path):
    engine, _, _ = build_engine(tmp_path)

    with pytest.raises(AppError) as excinfo:
        await engine.get_session(uuid4())

    assert excinfo.value.code == "session_not_found"


async def test_happy_turn_merges_fields_and_emits_a_fresh_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request())

    assert response.revision == 1
    assert response.status == "awaiting_confirmation"
    assert response.slots == FULL_SLOTS
    assert response.pending_clarifications == {}
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.reply == SUMMARY + "\nPlease confirm these details or tell me what to change."
    assert response.citations == []
    assert response.message_id != created.messages[0].id

    provider.assert_exhausted()
    method, context = provider.calls[0]
    assert method == "extract"
    assert context.current_message == "NS-000123 arrived broken and this is urgent"
    assert context.status == "collecting"
    assert context.slots == make_slots()
    assert context.pending_clarifications == {}
    assert [message.text for message in context.recent_messages] == [GREETING]

    stored = await repository.load(created.session_id)
    assert stored.revision == 1
    assert stored.collection_turn_count == 1
    assert stored.status == "awaiting_confirmation"
    assert stored.summary == SUMMARY
    assert stored.pending_review_revision == 1
    assert len(stored.messages) == 3
    assert stored.messages[1].role == "user"
    assert stored.messages[1].text == "NS-000123 arrived broken and this is urgent"
    assert stored.messages[2].role == "assistant"
    assert stored.messages[2].text == response.reply
    assert stored.messages[2].id == response.message_id
    assert len(stored.requests) == 1
    assert stored.requests[0].response == response


async def test_identical_request_replay_returns_the_stored_response_without_provider(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    command = turn_request()

    first = await engine.turn(created.session_id, command)
    provider.assert_exhausted()

    replay = await engine.turn(created.session_id, command)

    assert replay == first
    stored = await repository.load(created.session_id)
    assert stored.revision == 1
    assert len(stored.messages) == 3
    assert len(stored.requests) == 1


async def test_reused_request_id_with_changed_body_is_request_id_conflict(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    command = turn_request()
    await engine.turn(created.session_id, command)
    provider.assert_exhausted()

    changed = turn_request(text="NS-000123 arrived broken", request_id=command.request_id)

    with pytest.raises(AppError) as excinfo:
        await engine.turn(created.session_id, changed)

    assert excinfo.value.code == "request_id_conflict"
    assert excinfo.value.status == 409
    stored = await repository.load(created.session_id)
    assert stored.revision == 1
    assert len(stored.requests) == 1


async def test_stale_expected_revision_is_revision_conflict_with_current_revision(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    await engine.turn(created.session_id, turn_request())
    provider.assert_exhausted()

    with pytest.raises(AppError) as excinfo:
        await engine.turn(created.session_id, turn_request(revision=0))

    assert excinfo.value.code == "revision_conflict"
    assert excinfo.value.status == 409
    assert excinfo.value.current_revision == 1
    stored = await repository.load(created.session_id)
    assert stored.revision == 1
