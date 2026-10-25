"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): review."""

from __future__ import annotations

from uuid import uuid4

import pytest

from gateway.conversation.engine import ConversationEngine
from gateway.errors import AppError
from gateway.schemas import Citation, ConfirmRequest, KnowledgeAnswer
from gateway.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_record,
    make_session,
    make_slots,
)
from tests.unit.gateway.conversation.conversation_support import (
    FULL_EXTRACTION,
    FULL_SLOTS,
    SUMMARY,
    build_engine,
    confirm_request,
    turn_request,
)


async def test_dedicated_confirmation_completes_with_receipt_and_no_provider_call(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    provider.assert_exhausted()

    response = await engine.confirm(sid, confirm_request())

    assert response.revision == 2
    assert response.status == "completed"
    assert response.reply == (
        f"Saved locally as {sid}. No request has been sent to a real support team."
    )
    assert response.slots == FULL_SLOTS
    assert response.summary == SUMMARY
    assert response.pending_review_revision is None
    assert response.citations == []

    stored = await repository.load(sid)
    assert stored.status == "completed"
    assert stored.revision == 2
    assert stored.summary == SUMMARY
    assert stored.collection_turn_count == 1
    assert stored.confirmation is not None
    assert stored.confirmation.review_revision == 1
    assert [message.text for message in stored.messages[-2:]] == [
        "Confirm these details",
        response.reply,
    ]
    assert stored.messages[-2].role == "user"
    assert stored.messages[-2].input_mode == "system"
    assert stored.messages[-1].role == "assistant"
    assert stored.messages[-1].id == response.message_id
    assert stored.requests[-1].response == response
    provider.assert_exhausted()


async def test_identical_confirmation_replay_is_returned_without_mutation(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    command = confirm_request()

    first = await engine.confirm(sid, command)
    replay = await engine.confirm(sid, command)

    assert replay == first
    stored = await repository.load(sid)
    assert stored.revision == 2
    assert len(stored.messages) == 5
    assert len(stored.requests) == 2
    provider.assert_exhausted()


async def test_confirmation_with_stale_review_token_is_stale_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(review_revision=0))

    assert excinfo.value.code == "stale_review"
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    assert stored.revision == 1


async def test_confirmation_with_ineligible_review_is_review_not_ready(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=1,
            status="awaiting_confirmation",
            slots=FULL_SLOTS,
            pending_review_revision=1,
            review_rejected=True,
            summary=SUMMARY,
        )
    )
    provider = FakeProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request())

    assert excinfo.value.code == "review_not_ready"
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    assert stored.revision == 1
    provider.assert_exhausted()


async def test_confirmation_without_a_review_token_is_review_not_ready(tmp_path):
    """No token means no review exists, so the confirm is not ready, not stale."""

    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    session = make_session(
        session_id=sid,
        revision=1,
        status="collecting",
        slots=make_slots(order_number="NS-000123"),
    )
    await repository.save(session)
    provider = FakeProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=1, review_revision=0))

    assert excinfo.value.code == "review_not_ready"
    assert excinfo.value.status == 409
    assert await repository.load(sid) == session
    provider.assert_exhausted()


async def test_confirmation_reused_request_id_with_changed_body_is_conflict(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    command = confirm_request()
    await engine.confirm(sid, command)

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(
            sid,
            ConfirmRequest(
                request_id=command.request_id,
                expected_revision=1,
                review_revision=2,
            ),
        )

    assert excinfo.value.code == "request_id_conflict"


async def test_pure_question_answers_with_retrieved_sources_and_citations(
    tmp_path,
):
    record = make_record(
        record_id="faq-returns",
        title="Returns and refunds",
        keywords=["return", "returns", "refund", "refunds"],
        body="An unused item can be requested for return within 30 calendar days.",
    )
    answer = KnowledgeAnswer(
        answerable=True,
        answer=(
            "An unused item in its original packaging can be requested for return "
            "within 30 calendar days; refund eligibility requires a support review."
        ),
        cited_ids=["faq-returns"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            ("answer_knowledge", answer),
        ],
        kb_answers={"Can I return an unused item?": [record]},
    )
    created = await engine.create_session()
    sid = created.session_id

    response = await engine.turn(sid, turn_request("Can I return an unused item?"))

    expected_citation = Citation(
        source_id="faq-returns",
        title="Returns and refunds",
        excerpt=record.body,
        kb_version="2026-10-22",
    )
    assert response.revision == 1
    assert response.status == "collecting"
    assert response.citations == [expected_citation]
    assert response.reply == answer.answer
    assert response.pending_review_revision is None
    assert response.summary is None

    method, context = provider.calls[-1]
    assert method == "answer_knowledge"
    assert context.question == "Can I return an unused item?"
    assert context.kb_version == "2026-10-22"
    assert context.sources == [record]

    stored = await repository.load(sid)
    assert stored.revision == 1
    assert stored.collection_turn_count == 1
    assert stored.messages[-1].citations == [expected_citation]
    assert stored.messages[-1].text == response.reply
    assert stored.slots == make_slots()
    provider.assert_exhausted()


async def test_pure_question_at_valid_review_preserves_summary_and_token(
    tmp_path,
):
    record = make_record()
    answer = KnowledgeAnswer(
        answerable=True,
        answer="You can request a return within 30 calendar days.",
        cited_ids=["faq-returns"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction(intent="question", has_question=True)),
            ("answer_knowledge", answer),
        ],
        kb_answers={"Can I return it?": [record]},
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(
        sid, turn_request("Can I return it?", revision=1)
    )

    assert response.revision == 2
    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.slots == FULL_SLOTS
    assert response.reply == answer.answer
    assert len(response.citations) == 1
    stored = await repository.load(sid)
    assert stored.collection_turn_count == 2
    assert stored.pending_review_revision == 1
    assert stored.summary == SUMMARY
    provider.assert_exhausted()
