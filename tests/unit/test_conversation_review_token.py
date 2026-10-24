"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): review token."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.conversation import ConversationEngine
from app.errors import AppError
from app.prompts import ABSTENTION
from app.schemas import ConfirmRequest, KnowledgeAnswer
from app.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_record,
    make_session,
    make_slots,
)
from tests.unit.conversation_support import (
    FULL_EXTRACTION,
    FULL_SLOTS,
    SUMMARY,
    build_engine,
    confirm_equal,
    confirm_request,
    rejection,
    turn_request,
)


QUESTION = "Can I return it?"


RETURN_RECORD = make_record()


RETURN_ANSWER = KnowledgeAnswer(
    answerable=True,
    answer="You can request a return within 30 calendar days.",
    cited_ids=["faq-returns"],
)


async def reviewed_session_after_a_pure_question(tmp_path):
    """FULL review at revision 1, then a pure policy question at revision 2."""

    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction(intent="question", has_question=True)),
            ("answer_knowledge", RETURN_ANSWER),
        ],
        kb_answers={QUESTION: [RETURN_RECORD]},
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    question = await engine.turn(sid, turn_request(QUESTION, revision=1))
    assert question.revision == 2
    assert question.status == "awaiting_confirmation"
    assert question.pending_review_revision == 1
    return engine, repository, provider, sid


async def test_dedicated_confirm_after_a_pure_question_at_review_completes(tmp_path):
    engine, repository, provider, sid = await reviewed_session_after_a_pure_question(
        tmp_path
    )

    response = await engine.confirm(
        sid, confirm_request(revision=2, review_revision=1)
    )

    assert response.status == "completed"
    assert response.revision == 3
    assert response.reply == (
        f"Saved locally as {sid}. No request has been sent to a real support team."
    )
    assert response.slots == FULL_SLOTS
    assert response.summary == SUMMARY
    assert response.pending_review_revision is None
    stored = await repository.load(sid)
    assert stored.status == "completed"
    assert stored.revision == 3
    assert stored.collection_turn_count == 2
    assert stored.summary == SUMMARY
    assert stored.confirmation.review_revision == 1
    provider.assert_exhausted()


async def test_natural_confirmation_after_a_pure_question_at_review_completes(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction(intent="question", has_question=True)),
            ("answer_knowledge", RETURN_ANSWER),
            ("extract", confirm_equal()),
        ],
        kb_answers={QUESTION: [RETURN_RECORD]},
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    await engine.turn(sid, turn_request(QUESTION, revision=1))

    response = await engine.turn(sid, turn_request("Yes, that is right", revision=2))

    assert response.status == "completed"
    assert response.revision == 3
    assert response.reply == (
        f"Saved locally as {sid}. No request has been sent to a real support team."
    )
    assert response.slots == FULL_SLOTS
    assert response.summary == SUMMARY
    assert response.pending_review_revision is None
    stored = await repository.load(sid)
    assert stored.status == "completed"
    assert stored.revision == 3
    assert stored.collection_turn_count == 2
    assert stored.confirmation.review_revision == 1
    provider.assert_exhausted()


async def test_dedicated_confirm_after_an_unrelated_reply_at_review_completes(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction(intent="unrelated")),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    unrelated = await engine.turn(sid, turn_request("Nice weather", revision=1))
    assert unrelated.revision == 2
    assert unrelated.status == "awaiting_confirmation"
    assert unrelated.pending_review_revision == 1

    response = await engine.confirm(
        sid, confirm_request(revision=2, review_revision=1)
    )

    assert response.status == "completed"
    assert response.revision == 3
    assert response.summary == SUMMARY
    stored = await repository.load(sid)
    assert stored.status == "completed"
    assert stored.confirmation.review_revision == 1
    provider.assert_exhausted()


async def test_dedicated_confirm_after_a_pure_question_with_a_wrong_token_is_stale(
    tmp_path,
):
    engine, repository, provider, sid = await reviewed_session_after_a_pure_question(
        tmp_path
    )

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=2, review_revision=0))

    assert excinfo.value.code == "stale_review"
    assert excinfo.value.status == 409
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    assert stored.revision == 2
    assert stored.pending_review_revision == 1
    provider.assert_exhausted()


async def test_confirm_with_a_preserved_token_but_incomplete_slots_is_not_ready(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="awaiting_confirmation",
            slots=make_slots(order_number="NS-000123"),
            pending_review_revision=1,
            summary=SUMMARY,
        )
    )
    provider = FakeProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=2, review_revision=1))

    assert excinfo.value.code == "review_not_ready"
    assert excinfo.value.status == 409
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    assert stored.revision == 2
    provider.assert_exhausted()


async def test_confirm_with_a_preserved_token_but_a_pending_issue_is_not_ready(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="awaiting_confirmation",
            slots=FULL_SLOTS,
            pending_clarifications={
                "urgency": {
                    "code": "ambiguous",
                    "candidate": None,
                    "question": "Please clarify your urgency.",
                }
            },
            pending_review_revision=1,
            summary=SUMMARY,
        )
    )
    provider = FakeProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=2, review_revision=1))

    assert excinfo.value.code == "review_not_ready"
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    provider.assert_exhausted()


async def test_confirm_with_a_preserved_token_outside_a_review_is_not_ready(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="collecting",
            slots=FULL_SLOTS,
            pending_review_revision=1,
            summary=SUMMARY,
        )
    )
    provider = FakeProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=2, review_revision=1))

    assert excinfo.value.code == "review_not_ready"
    stored = await repository.load(sid)
    assert stored.status == "collecting"
    provider.assert_exhausted()


async def test_turn_limit_allows_a_pure_confirmation_of_a_preserved_review(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="awaiting_confirmation",
            slots=FULL_SLOTS,
            pending_review_revision=1,
            summary=SUMMARY,
            collection_turn_count=50,
        )
    )
    provider = FakeProvider([("extract", confirm_equal())])
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    assert response.status == "completed"
    assert response.revision == 3
    stored = await repository.load(sid)
    assert stored.collection_turn_count == 50
    assert stored.confirmation.review_revision == 1
    provider.assert_exhausted()


async def test_turn_limit_rejects_a_question_at_a_preserved_review(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="awaiting_confirmation",
            slots=FULL_SLOTS,
            pending_review_revision=1,
            summary=SUMMARY,
            collection_turn_count=50,
        )
    )
    provider = FakeProvider(
        [("extract", make_extraction(intent="question", has_question=True))]
    )
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request(QUESTION, revision=2))

    assert excinfo.value.code == "turn_limit"
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    assert stored.revision == 2
    assert stored.pending_review_revision == 1
    provider.assert_exhausted()


async def test_model_abstention_uses_deterministic_text_without_citations(
    tmp_path,
):
    record = make_record(
        record_id="faq-delivery",
        title="Delivery times",
        keywords=["delivery", "shipping", "ship", "arrive", "arrives"],
        body="Standard delivery takes 2 to 5 business days after dispatch.",
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(answerable=False, answer="", cited_ids=[]),
            ),
        ],
        kb_answers={"Do you ship internationally?": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Do you ship internationally?")
    )

    assert response.citations == []
    assert response.reply == ABSTENTION
    provider.assert_exhausted()


@pytest.mark.parametrize(
    "answer",
    [
        KnowledgeAnswer(answerable=True, answer="Grounded.", cited_ids=["faq-unknown"]),
        KnowledgeAnswer(
            answerable=True, answer="Grounded.", cited_ids=["faq-returns", "faq-returns"]
        ),
        KnowledgeAnswer(answerable=True, answer="x" * 1201, cited_ids=["faq-returns"]),
        KnowledgeAnswer(answerable=True, answer="", cited_ids=["faq-returns"]),
        KnowledgeAnswer(answerable=True, answer="Grounded.", cited_ids=[]),
        KnowledgeAnswer(answerable=False, answer="text", cited_ids=[]),
        KnowledgeAnswer(answerable=False, answer="", cited_ids=["faq-returns"]),
    ],
)
async def test_invalid_knowledge_answer_rolls_back_the_whole_turn(tmp_path, answer):
    record = make_record()
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

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request("Can I return an unused item?"))

    assert excinfo.value.code == "invalid_provider_response"
    stored = await repository.load(sid)
    assert stored.revision == 0
    assert stored.collection_turn_count == 0
    assert len(stored.messages) == 1
    assert stored.requests == []


async def test_turn_limit_blocks_without_a_provider_call_or_mutation(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    session = make_session(session_id=sid, revision=5, collection_turn_count=50)
    await repository.save(session)
    provider = FakeProvider()
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request("NS-000123 arrived broken", revision=5))

    assert excinfo.value.code == "turn_limit"
    assert excinfo.value.status == 409
    stored = await repository.load(sid)
    assert stored == session
    provider.assert_exhausted()


async def test_turn_limit_still_allows_a_pure_confirmation_of_a_valid_review(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=3,
            status="awaiting_confirmation",
            slots=FULL_SLOTS,
            pending_review_revision=3,
            summary=SUMMARY,
            collection_turn_count=50,
        )
    )
    provider = FakeProvider([("extract", confirm_equal())])
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    response = await engine.turn(sid, turn_request("Yes", revision=3))

    assert response.status == "completed"
    assert response.revision == 4
    stored = await repository.load(sid)
    assert stored.collection_turn_count == 50
    assert stored.confirmation is not None
    provider.assert_exhausted()


async def test_turn_limit_rejects_nonconfirmation_without_mutation(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    session = make_session(
        session_id=sid,
        revision=3,
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        pending_review_revision=3,
        summary=SUMMARY,
        collection_turn_count=50,
    )
    await repository.save(session)
    provider = FakeProvider([("extract", make_extraction(intent="unrelated"))])
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request("Something else", revision=3))

    assert excinfo.value.code == "turn_limit"
    assert await repository.load(sid) == session
    provider.assert_exhausted()


async def test_completed_session_rejects_new_commands(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    await engine.confirm(sid, confirm_request())

    with pytest.raises(AppError) as turn_error:
        await engine.turn(sid, turn_request("Another change", revision=2))
    assert turn_error.value.code == "session_completed"

    with pytest.raises(AppError) as confirm_error:
        await engine.confirm(
            sid,
            ConfirmRequest(
                request_id=uuid4(), expected_revision=2, review_revision=1
            ),
        )
    assert confirm_error.value.code == "session_completed"
    provider.assert_exhausted()


async def test_completed_session_still_replays_identical_commands(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    command = turn_request()
    turn_response = await engine.turn(sid, command)
    confirm_command = confirm_request()
    confirm_response = await engine.confirm(sid, confirm_command)

    assert await engine.turn(sid, command) == turn_response
    assert await engine.confirm(sid, confirm_command) == confirm_response
    stored = await repository.load(sid)
    assert stored.revision == 2
    assert len(stored.requests) == 2
    provider.assert_exhausted()


async def test_atomic_save_failure_keeps_prior_bytes_and_no_replay_record(
    tmp_path, monkeypatch
):
    import app.storage as storage_module

    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    original = (tmp_path / f"{sid}.json").read_bytes()

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(AppError) as excinfo:
        await engine.turn(sid, turn_request())

    assert excinfo.value.code == "storage_unavailable"
    assert (tmp_path / f"{sid}.json").read_bytes() == original
    stored = await repository.load(sid)
    assert stored.revision == 0
    assert len(stored.messages) == 1
    assert stored.requests == []
    assert stored.collection_turn_count == 0


async def test_engine_lock_timeout_is_session_busy_without_provider_call(tmp_path):
    engine, repository, provider = build_engine(tmp_path)
    created = await engine.create_session()
    sid = created.session_id

    async with repository.locked(sid):
        with pytest.raises(AppError) as excinfo:
            await engine.turn(sid, turn_request())

    assert excinfo.value.code == "session_busy"
    assert excinfo.value.status == 429
    assert excinfo.value.retryable is True
    provider.assert_exhausted()
    stored = await repository.load(sid)
    assert stored.revision == 0


async def test_transient_recovery_marker_is_never_persisted(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            (
                "extract",
                make_extraction(
                    intent="correction",
                    candidates={"urgency": "low"},
                    explicit_corrections=["urgency"],
                ),
            ),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    await engine.turn(sid, turn_request("No", revision=1))
    await engine.turn(sid, turn_request("Make it low", revision=2))

    stored = await repository.load(sid)
    assert stored.review_rejected is False
    assert "recovered_this_turn" not in stored.model_dump()
    provider.assert_exhausted()
