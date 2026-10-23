"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.conversation import ConversationEngine
from app.errors import AppError
from app.prompts import (
    ABSTENTION,
    CAPABILITY,
    CAPABILITY_REPLY,
    GREETING,
    INVALID_ORDER,
    MISSING_CATEGORY,
    MISSING_ORDER,
    NO_REVIEW_TO_REJECT,
    NOTHING_TO_CONFIRM,
    POLICY_QUESTION_SUFFIX,
    REJECT_PROMPT,
    SOCIAL_ACKNOWLEDGMENT,
)
from app.schemas import Citation, ConfirmRequest, KnowledgeAnswer, TurnRequest
from app.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    SlowProvider,
    make_extraction,
    make_record,
    make_session,
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
    "Order: NS-000123\n"
    "Category: damaged_item\n"
    "Problem: My order arrived broken\n"
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


# --- Eight required rejection-recovery regressions (IC §5) -------------------


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


async def test_reject_then_valid_correction_emits_fresh_review_then_confirm(
    tmp_path,
):
    correction = make_extraction(
        intent="correction",
        candidates={"urgency": "low"},
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                          ("extract", correction), ("extract", confirm_equal("low"))]
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )

    second = await engine.turn(sid, turn_request("No, that's wrong", revision=1))
    await assert_state(
        repository, sid, second, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert second.reply == "What would you like to change?"

    third = await engine.turn(sid, turn_request("Make the urgency low", revision=2))
    corrected_summary = SUMMARY.replace("Urgency: high", "Urgency: low")
    await assert_state(
        repository, sid, third, revision=3, status="awaiting_confirmation",
        rejected=False, token=3, summary=corrected_summary,
    )
    assert third.reply == corrected_summary + "\nPlease confirm these details or tell me what to change."

    fourth = await engine.turn(sid, turn_request("Yes, confirm", revision=3))
    await assert_state(
        repository, sid, fourth, revision=4, status="completed",
        rejected=False, token=None, summary=corrected_summary,
    )
    assert fourth.reply == f"Saved locally as {sid}. No request has been sent to a real support team."
    stored = await repository.load(sid)
    assert stored.collection_turn_count == 3
    assert stored.confirmation is not None
    assert stored.confirmation.review_revision == 3
    provider.assert_exhausted()


async def test_reject_then_equal_value_reaffirmation_recovers_and_confirms(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", confirm_equal()), ("extract", confirm_equal())],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    rejected = await engine.turn(sid, turn_request("That is not right", revision=1))
    await assert_state(
        repository, sid, rejected, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )

    reaffirmed = await engine.turn(sid, turn_request("It is correct", revision=2))
    await assert_state(
        repository, sid, reaffirmed, revision=3, status="awaiting_confirmation",
        rejected=False, token=3, summary=SUMMARY,
    )

    confirmed = await engine.turn(sid, turn_request("Yes", revision=3))
    await assert_state(
        repository, sid, confirmed, revision=4, status="completed",
        rejected=False, token=None, summary=SUMMARY,
    )
    provider.assert_exhausted()


async def test_reject_then_no_candidate_reaffirmation_recovers_and_confirms(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", make_extraction(intent="confirm")),
                ("extract", make_extraction(intent="confirm"))],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    rejected = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository, sid, rejected, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )

    reaffirmed = await engine.turn(sid, turn_request("Correct", revision=2))
    await assert_state(
        repository, sid, reaffirmed, revision=3, status="awaiting_confirmation",
        rejected=False, token=3, summary=SUMMARY,
    )

    confirmed = await engine.turn(sid, turn_request("Yes, that is right", revision=3))
    await assert_state(
        repository, sid, confirmed, revision=4, status="completed",
        rejected=False, token=None, summary=SUMMARY,
    )
    provider.assert_exhausted()


async def test_reject_then_invalid_correction_stays_rejected(tmp_path):
    invalid = make_extraction(
        intent="correction",
        candidates={"urgency": "super-urgent"},
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", invalid)],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository, sid, second, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )
    third = await engine.turn(sid, turn_request("Make it super urgent", revision=2))

    await assert_state(
        repository, sid, third, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert third.reply == "Please choose low, normal, or high urgency."
    provider.assert_exhausted()


async def test_reject_then_valid_correction_with_invalid_field_clears_flag_but_waits(
    tmp_path,
):
    mixed = make_extraction(
        intent="correction",
        candidates={"urgency": "low", "category": "mystery"},
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", mixed)],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository, sid, second, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )
    third = await engine.turn(sid, turn_request("Make urgency low, category mystery", revision=2))

    await assert_state(
        repository, sid, third, revision=3, status="collecting",
        rejected=False, token=None, summary=None,
    )
    assert third.reply == "Please choose delivery, damaged item, wrong item, return/refund, or other."
    provider.assert_exhausted()


async def test_reject_then_unrelated_statement_stays_rejected(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", make_extraction(intent="unrelated"))],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository, sid, second, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )
    third = await engine.turn(sid, turn_request("The weather is nice", revision=2))

    await assert_state(
        repository, sid, third, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert third.reply == CAPABILITY
    provider.assert_exhausted()


async def test_reject_then_pure_question_stays_rejected(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", make_extraction(intent="question", has_question=True))],
        kb_answers={"Do you ship internationally?": []},
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository, sid, second, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )
    third = await engine.turn(sid, turn_request("Do you ship internationally?", revision=2))

    await assert_state(
        repository, sid, third, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert third.reply == (
        "I do not have that information in the Northstar Shop knowledge base. "
        "I can still record your issue for review."
    )
    assert third.citations == []
    provider.assert_exhausted()


async def test_reject_then_question_with_facts_stays_rejected_and_appends_suffix(
    tmp_path,
):
    question_with_facts = make_extraction(
        intent="provide_details",
        has_question=True,
        candidates={"order_number": "NS-000123"},
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()),
                ("extract", question_with_facts)],
    )
    created = await engine.create_session()
    sid = created.session_id

    first = await engine.turn(sid, turn_request())
    await assert_state(
        repository, sid, first, revision=1, status="awaiting_confirmation",
        rejected=False, token=1, summary=SUMMARY,
    )
    second = await engine.turn(sid, turn_request("No", revision=1))
    await assert_state(
        repository, sid, second, revision=2, status="collecting",
        rejected=True, token=None, summary=None,
    )
    third = await engine.turn(sid, turn_request("NS-000123 — can I return it?", revision=2))

    await assert_state(
        repository, sid, third, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert third.reply == (
        "What would you like to change? "
        "Please ask your policy question in a separate message."
    )
    provider.assert_exhausted()


# --- Review, confirmation, RAG citations, limits, durability -----------------


def confirm_request(revision=1, review_revision=1, request_id=None):
    return ConfirmRequest(
        request_id=request_id or uuid4(),
        expected_revision=revision,
        review_revision=review_revision,
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
        "Confirm reviewed intake",
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


# --- A preserved review token stays current across unrelated turns ----------
# BS §5.7 / IC §13.3 J08: unrelated questions preserve the token but advance the
# revision, so staleness is decided by the token, never by `revision`.

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


# --- Speech message lookup and the shared text-turn deadline -----------------


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
    import app.conversation as conversation_module

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
    import app.conversation as conversation_module

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


# --- Mutation hardening: boundary scenarios required by IC §5 and RAG §4 -----


def canonical_digest(endpoint, session_id, body):
    import hashlib
    import json

    payload = {"endpoint": endpoint, "session_id": str(session_id), **body}
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


async def test_turn_digest_is_canonical_json_with_endpoint_and_request_body():
    from app.conversation import turn_digest

    sid = UUID("8b9dc0ee-1111-4a4a-8a8a-0123456789ab")
    command = TurnRequest(
        request_id=UUID("11111111-2222-4333-8444-555555555555"),
        expected_revision=2,
        text="Café — naïve",
        input_mode="transcript",
    )
    expected = canonical_digest(
        "turn",
        sid,
        {
            "request_id": "11111111-2222-4333-8444-555555555555",
            "expected_revision": 2,
            "text": "Café — naïve",
            "input_mode": "transcript",
        },
    )

    assert turn_digest(sid, command) == expected


async def test_confirm_digest_is_canonical_json_with_endpoint_and_tokens():
    from app.conversation import confirm_digest

    sid = UUID("8b9dc0ee-1111-4a4a-8a8a-0123456789ab")
    command = ConfirmRequest(
        request_id=UUID("11111111-2222-4333-8444-555555555555"),
        expected_revision=3,
        review_revision=2,
    )
    expected = canonical_digest(
        "confirm",
        sid,
        {
            "request_id": "11111111-2222-4333-8444-555555555555",
            "expected_revision": 3,
            "review_revision": 2,
        },
    )

    assert confirm_digest(sid, command) == expected


async def test_unrelated_statement_without_facts_uses_capability_reply(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", make_extraction(intent="unrelated"))]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Hello there"))

    assert response.revision == 1
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == (
        "I can record an order issue and summarize it. I cannot verify orders, "
        "issue refunds, or contact a support team."
    )
    stored = await repository.load(created.session_id)
    assert stored.collection_turn_count == 1
    provider.assert_exhausted()


async def test_unrelated_statement_at_valid_review_preserves_summary_and_token(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", make_extraction(intent="unrelated"))],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Nice weather", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.reply == (
        "I can record an order issue and summarize it. I cannot verify orders, "
        "issue refunds, or contact a support team."
    )
    provider.assert_exhausted()


async def test_empty_extraction_at_valid_review_redisplays_the_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction()),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Hmm", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.reply == SUMMARY + "\nPlease confirm these details or tell me what to change."
    provider.assert_exhausted()


async def test_empty_extraction_with_complete_slots_creates_a_fresh_review(
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
            collection_turn_count=2,
        )
    )
    provider = FakeProvider([("extract", make_extraction())])
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())

    response = await engine.turn(sid, turn_request("Anything", revision=2))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 3
    assert response.summary == SUMMARY
    assert response.reply == SUMMARY + "\nPlease confirm these details or tell me what to change."
    provider.assert_exhausted()


async def test_reject_without_a_review_keeps_the_pending_issue_and_no_flag(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="reject", candidates={"urgency": "super-urgent"}
                ),
            )
        ],
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("No, it is super urgent"))

    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == NO_REVIEW_TO_REJECT
    assert response.pending_clarifications["urgency"].code == "invalid"
    stored = await repository.load(created.session_id)
    assert stored.review_rejected is False
    provider.assert_exhausted()


async def test_pure_question_on_a_complete_session_without_review_has_no_follow_up(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=1,
            status="collecting",
            slots=FULL_SLOTS,
            collection_turn_count=1,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", make_extraction(intent="question", has_question=True))]),
        FakeKnowledgeBase({"Which payment methods are accepted?": []}),
    )

    response = await engine.turn(
        sid, turn_request("Which payment methods are accepted?", revision=1)
    )

    assert response.reply == (
        "I do not have that information in the Northstar Shop knowledge base. "
        "I can still record your issue for review."
    )
    assert response.status == "collecting"


async def test_unrelated_question_without_facts_uses_the_capability_reply(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="unrelated", has_question=True)),
        ],
        kb_answers={"What can you do?": []},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("What can you do?"))

    assert response.reply == CAPABILITY
    assert ABSTENTION not in response.reply
    assert POLICY_QUESTION_SUFFIX not in response.reply
    assert [method for method, _ in provider.calls] == ["extract"]
    provider.assert_exhausted()


async def test_pure_question_during_intake_has_no_follow_up(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(candidates={"order_number": "123"}),
            ),
            ("extract", make_extraction(intent="question", has_question=True)),
        ],
        kb_answers={"Do you ship internationally?": []},
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request("My order is 123"))

    response = await engine.turn(
        sid, turn_request("Do you ship internationally?", revision=1)
    )

    assert response.reply == (
        "I do not have that information in the Northstar Shop knowledge base. "
        "I can still record your issue for review."
    )
    assert response.pending_clarifications["order_number"].code == "invalid"
    provider.assert_exhausted()


async def test_question_with_only_corrections_is_not_a_pure_question(tmp_path):
    extraction = make_extraction(
        intent="unrelated",
        has_question=True,
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Why urgent?"))

    assert response.reply == (
        "What is your order number? Use NS- followed by six digits, for example NS-123456. "
        "Please ask your policy question in a separate message."
    )
    provider.assert_exhausted()


async def test_question_with_only_ambiguities_is_not_a_pure_question(tmp_path):
    extraction = make_extraction(
        intent="unrelated",
        has_question=True,
        ambiguous_fields=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Why urgent?"))

    assert response.reply == (
        "Please clarify your urgency. "
        "Please ask your policy question in a separate message."
    )
    provider.assert_exhausted()


async def test_one_character_answer_is_a_valid_grounded_answer(tmp_path):
    record = make_record()
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(answerable=True, answer="x", cited_ids=["faq-returns"]),
            ),
        ],
        kb_answers={"Can I return it?": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Can I return it?"))

    assert response.reply == "x"
    assert len(response.citations) == 1


async def test_answer_at_the_1200_character_boundary_is_accepted(tmp_path):
    record = make_record()
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True, answer="x" * 1200, cited_ids=["faq-returns"]
                ),
            ),
        ],
        kb_answers={"Can I return it?": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Can I return it?"))

    assert response.reply == "x" * 1200


async def test_two_distinct_cited_ids_are_accepted_and_snapshotted(tmp_path):
    first = make_record(record_id="faq-returns", title="Returns and refunds")
    second = make_record(
        record_id="faq-damaged",
        title="Damaged items",
        keywords=["damaged", "broken"],
        body="Keep the packaging and describe the damage.",
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True,
                    answer="Both policies apply.",
                    cited_ids=["faq-returns", "faq-damaged"],
                ),
            ),
        ],
        kb_answers={"Can I return a damaged item?": [first, second]},
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Can I return a damaged item?")
    )

    assert [citation.source_id for citation in response.citations] == [
        "faq-returns",
        "faq-damaged",
    ]
    assert response.citations[1].title == "Damaged items"
    assert response.citations[1].excerpt == second.body


async def test_three_cited_ids_are_an_invalid_provider_response(tmp_path):
    records = [
        make_record(record_id=f"faq-{index}") for index in range(3)
    ]
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            (
                "answer_knowledge",
                KnowledgeAnswer(
                    answerable=True,
                    answer="Grounded.",
                    cited_ids=["faq-0", "faq-1", "faq-2"],
                ),
            ),
        ],
        kb_answers={"Policies?": records},
    )
    created = await engine.create_session()

    with pytest.raises(AppError) as excinfo:
        await engine.turn(created.session_id, turn_request("Policies?"))

    assert excinfo.value.code == "invalid_provider_response"


async def rejected_after_full_review(tmp_path, script_tail):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", rejection()), *script_tail],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    await engine.turn(sid, turn_request("No", revision=1))
    return engine, repository, provider, sid


async def test_reaffirmation_with_an_explicit_correction_list_stays_rejected(
    tmp_path,
):
    extraction = make_extraction(
        intent="confirm",
        explicit_corrections=["urgency"],
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == "What would you like to change?"
    provider.assert_exhausted()


async def test_reaffirmation_with_a_pending_issue_stays_rejected(tmp_path):
    extraction = make_extraction(
        intent="confirm", candidates={"urgency": "bogus"}
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Yes, bogus", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == "Please choose low, normal, or high urgency."
    provider.assert_exhausted()


async def test_reaffirmation_with_incomplete_slots_stays_rejected(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="collecting",
            slots=make_slots(order_number="NS-000123"),
            review_rejected=True,
            collection_turn_count=2,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", make_extraction(intent="confirm"))]),
        FakeKnowledgeBase(),
    )

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == REJECT_PROMPT


async def test_reaffirmation_with_a_changed_candidate_stays_rejected(tmp_path):
    extraction = make_extraction(
        intent="confirm", candidates={"urgency": "low"}
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Actually low", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.pending_clarifications["urgency"].code == "conflict"
    provider.assert_exhausted()


async def test_reaffirmation_with_an_invalid_accepted_slot_stays_rejected(
    tmp_path,
):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=2,
            status="collecting",
            slots=FULL_SLOTS.model_copy(update={"urgency": "bogus"}),
            review_rejected=True,
            collection_turn_count=2,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", make_extraction(intent="confirm"))]),
        FakeKnowledgeBase(),
    )

    response = await engine.turn(sid, turn_request("Yes", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="collecting",
        rejected=True, token=None, summary=None,
    )
    assert response.reply == "What would you like to change?"


async def test_reaffirmation_with_alias_equal_candidates_recovers(tmp_path):
    extraction = make_extraction(
        intent="confirm",
        candidates={"category": "Damaged Item", "urgency": "High"},
    )
    engine, repository, provider, sid = await rejected_after_full_review(
        tmp_path, [("extract", extraction)]
    )

    response = await engine.turn(sid, turn_request("Yes, that is right", revision=2))

    await assert_state(
        repository, sid, response, revision=3, status="awaiting_confirmation",
        rejected=False, token=3, summary=SUMMARY,
    )
    provider.assert_exhausted()


async def test_confirmation_with_a_new_pending_issue_does_not_complete(tmp_path):
    extraction = make_extraction(
        intent="confirm", candidates={"urgency": "bogus"}
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", extraction)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Yes, bogus", revision=1))

    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.reply == "Please choose low, normal, or high urgency."
    stored = await repository.load(sid)
    assert stored.confirmation is None
    provider.assert_exhausted()


async def test_confirmation_with_alias_equal_candidates_completes(tmp_path):
    extraction = make_extraction(
        intent="confirm",
        candidates={
            "order_number": "NS-000123",
            "category": "Damaged Item",
            "description": "My order arrived broken",
            "urgency": "High",
        },
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", extraction)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Yes", revision=1))

    assert response.status == "completed"
    assert response.summary == SUMMARY
    stored = await repository.load(sid)
    assert stored.confirmation is not None
    provider.assert_exhausted()


async def test_confirmation_revision_conflict_exposes_current_revision(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    with pytest.raises(AppError) as excinfo:
        await engine.confirm(sid, confirm_request(revision=0, review_revision=1))

    assert excinfo.value.code == "revision_conflict"
    assert excinfo.value.current_revision == 1


async def test_get_session_exposes_the_review_token_and_summary(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    fetched = await engine.get_session(sid)

    assert fetched.revision == 1
    assert fetched.status == "awaiting_confirmation"
    assert fetched.pending_review_revision == 1
    assert fetched.summary == SUMMARY
    assert len(fetched.messages) == 3
    provider.assert_exhausted()


async def test_engine_locks_are_per_session_not_global(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    first = await engine.create_session()
    second = await engine.create_session()

    async with repository.locked(first.session_id):
        fetched = await engine.get_session(second.session_id)
        assert fetched.session_id == second.session_id
        assert await engine.assistant_text(
            second.session_id, second.messages[0].id
        ) == GREETING
        turned = await engine.turn(second.session_id, turn_request())
        assert turned.revision == 1
        confirmed = await engine.confirm(
            second.session_id, confirm_request(revision=1, review_revision=1)
        )
        assert confirmed.status == "completed"
        created = await engine.create_session()
        assert created.revision == 0
    provider.assert_exhausted()


async def test_dedicated_confirmation_persists_the_canonical_request_digest(
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

    stored = await repository.load(sid)
    expected = canonical_digest(
        "confirm",
        sid,
        {
            "request_id": str(command.request_id),
            "expected_revision": 1,
            "review_revision": 1,
        },
    )
    assert stored.requests[-1].input_digest == expected


async def test_accepted_equal_details_at_a_valid_review_create_a_fresh_review(
    tmp_path,
):
    repeated = make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": "high",
        }
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", repeated)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Same details again", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 2
    assert response.summary == SUMMARY
    provider.assert_exhausted()


async def test_confirmation_with_correction_flags_does_not_complete(tmp_path):
    extraction = make_extraction(
        intent="confirm",
        explicit_corrections=["urgency"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", extraction)],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Yes, but urgency", revision=1))

    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    stored = await repository.load(sid)
    assert stored.confirmation is None
    provider.assert_exhausted()


async def test_engine_operations_on_other_sessions_do_not_wait_for_a_slow_turn(
    tmp_path,
):
    import asyncio
    import time

    repository = JsonSessionRepository(tmp_path)
    provider = SlowProvider(
        [("extract", FULL_EXTRACTION), ("extract", FULL_EXTRACTION)], delay=0.4
    )
    engine = ConversationEngine(repository, provider, FakeKnowledgeBase())
    slow = await engine.create_session()
    other = await engine.create_session()
    await engine.turn(other.session_id, turn_request())
    await engine.confirm(
        other.session_id, confirm_request(revision=1, review_revision=1)
    )
    fast_engine = ConversationEngine(
        repository,
        FakeProvider([("extract", FULL_EXTRACTION)]),
        FakeKnowledgeBase(),
    )
    fresh = await fast_engine.create_session()

    slow_turn = asyncio.create_task(
        engine.turn(slow.session_id, turn_request())
    )
    await asyncio.sleep(0.05)

    started = time.monotonic()
    await engine.get_session(other.session_id)
    await engine.assistant_text(other.session_id, other.messages[0].id)
    await fast_engine.turn(fresh.session_id, turn_request())
    await fast_engine.confirm(
        fresh.session_id, confirm_request(revision=1, review_revision=1)
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    await slow_turn


async def test_engine_never_uses_a_global_none_lock_key(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", FULL_EXTRACTION)]
    )
    first = await engine.create_session()
    second = await engine.create_session()
    await engine.turn(second.session_id, turn_request())

    async with repository.locked(None):
        fetched = await engine.get_session(first.session_id)
        assert fetched.session_id == first.session_id
        assert await engine.assistant_text(
            first.session_id, first.messages[0].id
        ) == GREETING
        created = await engine.create_session()
        assert created.revision == 0
        confirmed = await engine.confirm(
            second.session_id, confirm_request(revision=1, review_revision=1)
        )
        assert confirmed.status == "completed"
    provider.assert_exhausted()


# --- Canonical intent table: side branches never prompt (IC §5) --------------
# social -> fixed acknowledgment, capability -> fixed offer, unrelated -> fixed
# capability reply. None retrieves, prompts a field, re-displays a review or
# starts intake, even when the literal has_question flag is true
# ("Hello, how are you?").


def social_extraction(has_question: bool = False):
    return make_extraction(intent="social", has_question=has_question)


def capability_extraction(has_question: bool = False):
    return make_extraction(intent="capability", has_question=has_question)


def start_intake_extraction(has_question: bool = False):
    return make_extraction(intent="start_intake", has_question=has_question)


async def test_pure_social_replies_with_the_exact_acknowledgment_only(tmp_path):
    for text, has_question in (
        ("Hello", False),
        ("Thank you", False),
        ("Hello, how are you?", True),
        ("How is your day?", True),
    ):
        engine, repository, provider = build_engine(
            tmp_path, script=[("extract", social_extraction(has_question))]
        )
        created = await engine.create_session()

        response = await engine.turn(created.session_id, turn_request(text))

        assert response.reply == SOCIAL_ACKNOWLEDGMENT
        assert ABSTENTION not in response.reply
        assert POLICY_QUESTION_SUFFIX not in response.reply
        assert response.citations == []
        assert response.status == "collecting"
        assert response.slots == make_slots()
        assert response.pending_clarifications == {}
        assert response.pending_review_revision is None
        assert response.summary is None
        assert [method for method, _ in provider.calls] == ["extract"]

        stored = await repository.load(created.session_id)
        assert stored.revision == 1
        assert stored.collection_turn_count == 1
        assert stored.messages[-2].text == text
        assert stored.messages[-1].text == SOCIAL_ACKNOWLEDGMENT
        assert stored.messages[-1].citations == []
        provider.assert_exhausted()


async def test_social_at_a_valid_review_preserves_the_review_without_a_reminder(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", social_extraction(True))],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Hello, how are you?", revision=1))

    assert response.reply == SOCIAL_ACKNOWLEDGMENT
    assert response.revision == 2
    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.slots == FULL_SLOTS
    assert response.citations == []
    stored = await repository.load(sid)
    assert stored.pending_review_revision == 1
    assert stored.summary == SUMMARY
    assert stored.review_rejected is False
    provider.assert_exhausted()


async def test_social_while_rejected_preserves_rejection_without_a_prompt(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", rejection()),
            ("extract", social_extraction()),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())
    await engine.turn(sid, turn_request("No", revision=1))

    response = await engine.turn(sid, turn_request("Thank you", revision=2))

    assert response.reply == SOCIAL_ACKNOWLEDGMENT
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    stored = await repository.load(sid)
    assert stored.review_rejected is True
    provider.assert_exhausted()


async def test_social_with_a_pending_issue_preserves_it_without_reasking(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(candidates={"order_number": "123"})),
            ("extract", social_extraction(True)),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request("My order is 123"))

    response = await engine.turn(sid, turn_request("Hello, how are you?", revision=1))

    assert response.reply == SOCIAL_ACKNOWLEDGMENT
    assert response.pending_clarifications["order_number"].code == "invalid"
    stored = await repository.load(sid)
    assert stored.pending_clarifications["order_number"].code == "invalid"
    provider.assert_exhausted()


async def test_social_with_facts_falls_through_to_normal_intake(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(intent="social", candidates={"order_number": "NS-000123"}),
            )
        ],
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Hi, my order is NS-000123")
    )

    assert response.reply == MISSING_CATEGORY
    assert SOCIAL_ACKNOWLEDGMENT not in response.reply
    assert response.slots.order_number == "NS-000123"
    provider.assert_exhausted()


async def test_social_on_a_complete_session_without_review_has_no_follow_up(tmp_path):
    repository = JsonSessionRepository(tmp_path)
    sid = uuid4()
    await repository.save(
        make_session(
            session_id=sid,
            revision=1,
            status="collecting",
            slots=FULL_SLOTS,
            collection_turn_count=1,
        )
    )
    engine = ConversationEngine(
        repository,
        FakeProvider([("extract", social_extraction())]),
        FakeKnowledgeBase(),
    )

    response = await engine.turn(sid, turn_request("Hello", revision=1))

    assert response.reply == SOCIAL_ACKNOWLEDGMENT
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    assert response.slots == FULL_SLOTS


@pytest.mark.parametrize(
    "text",
    ["Hello, how long does delivery take?", "Thank you, how long does delivery take?"],
)
async def test_social_preface_with_a_policy_question_still_uses_rag(tmp_path, text):
    record = make_record(record_id="faq-delivery", title="Delivery times")
    answer = KnowledgeAnswer(
        answerable=True,
        answer="Standard delivery takes 2 to 5 business days after dispatch.",
        cited_ids=["faq-delivery"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True)),
            ("answer_knowledge", answer),
        ],
        kb_answers={text: [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request(text))

    assert response.reply == answer.answer
    assert SOCIAL_ACKNOWLEDGMENT not in response.reply
    assert POLICY_QUESTION_SUFFIX not in response.reply
    assert response.citations[0].source_id == "faq-delivery"
    assert [method for method, _ in provider.calls] == ["extract", "answer_knowledge"]
    assert provider.calls[1][1].question == text
    provider.assert_exhausted()


async def test_capability_replies_with_the_exact_offer_and_no_retrieval(tmp_path):
    for has_question in (False, True):
        engine, repository, provider = build_engine(
            tmp_path, script=[("extract", capability_extraction(has_question))]
        )
        created = await engine.create_session()

        response = await engine.turn(
            created.session_id, turn_request("What can you help me with?")
        )

        assert response.reply == CAPABILITY_REPLY
        assert ABSTENTION not in response.reply
        assert POLICY_QUESTION_SUFFIX not in response.reply
        assert response.citations == []
        assert response.status == "collecting"
        assert response.slots == make_slots()
        assert response.pending_clarifications == {}
        assert response.pending_review_revision is None
        assert response.summary is None
        assert [method for method, _ in provider.calls] == ["extract"]

        stored = await repository.load(created.session_id)
        assert stored.revision == 1
        assert stored.collection_turn_count == 1
        assert stored.status == "collecting"
        assert stored.slots == make_slots()
        provider.assert_exhausted()


async def test_capability_at_a_valid_review_preserves_the_token(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", capability_extraction(True))],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(
        sid, turn_request("What can you help me with?", revision=1)
    )

    assert response.reply == CAPABILITY_REPLY
    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    assert response.slots == FULL_SLOTS
    assert response.citations == []
    stored = await repository.load(sid)
    assert stored.pending_review_revision == 1
    provider.assert_exhausted()


async def test_capability_with_facts_falls_through_to_intake(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="capability",
                    candidates={"order_number": "NS-000123"},
                ),
            )
        ],
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("What can you do? My order is NS-000123")
    )

    assert response.reply == MISSING_CATEGORY
    assert CAPABILITY_REPLY not in response.reply
    assert response.slots.order_number == "NS-000123"
    provider.assert_exhausted()


async def test_unrelated_with_a_question_flag_never_retrieves(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", make_extraction(intent="unrelated", has_question=True))],
        kb_answers={"Tell me a joke": []},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Tell me a joke"))

    assert response.reply == CAPABILITY
    assert ABSTENTION not in response.reply
    assert POLICY_QUESTION_SUFFIX not in response.reply
    assert response.citations == []
    assert response.slots == make_slots()
    assert [method for method, _ in provider.calls] == ["extract"]
    provider.assert_exhausted()


async def test_start_intake_asks_the_first_pending_clarification(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(candidates={"order_number": "123"})),
            ("extract", start_intake_extraction()),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request("My order is 123"))

    response = await engine.turn(
        sid, turn_request("I want to report an issue", revision=1)
    )

    assert response.reply == INVALID_ORDER
    assert response.pending_clarifications["order_number"].code == "invalid"
    assert response.slots == make_slots()
    provider.assert_exhausted()


async def test_start_intake_asks_the_first_missing_field_without_fabricated_facts(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", start_intake_extraction(True))]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("I want to report an issue")
    )

    assert response.reply == f"{MISSING_ORDER} {POLICY_QUESTION_SUFFIX}"
    assert response.slots == make_slots()
    assert response.pending_clarifications == {}
    assert response.pending_review_revision is None
    assert response.summary is None
    assert [method for method, _ in provider.calls] == ["extract"]
    stored = await repository.load(created.session_id)
    assert stored.slots == make_slots()
    provider.assert_exhausted()


async def test_start_intake_resumes_the_current_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", FULL_EXTRACTION), ("extract", start_intake_extraction())],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("Continue my issue", revision=1))

    assert response.reply == (
        SUMMARY + "\nPlease confirm these details or tell me what to change."
    )
    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    provider.assert_exhausted()


async def test_confirm_without_a_review_explains_and_never_completes(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", make_extraction(intent="confirm"))]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Yes, that is right")
    )

    assert response.reply == NOTHING_TO_CONFIRM
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    stored = await repository.load(created.session_id)
    assert stored.status == "collecting"
    assert stored.confirmation is None
    provider.assert_exhausted()


async def test_reject_without_a_review_does_not_fabricate_rejection_state(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", make_extraction(intent="reject"))]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("No, those details are wrong")
    )

    assert response.reply == NO_REVIEW_TO_REJECT
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    stored = await repository.load(created.session_id)
    assert stored.review_rejected is False
    provider.assert_exhausted()


async def test_facts_with_a_policy_question_append_the_suffix_without_an_answer_call(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="provide_details",
                    has_question=True,
                    candidates={"order_number": "NS-000123"},
                ),
            )
        ],
        kb_answers={"Can I return it?": []},
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("My order NS-000123 broke; can I return it?")
    )

    assert response.reply == f"{MISSING_CATEGORY} {POLICY_QUESTION_SUFFIX}"
    assert response.slots.order_number == "NS-000123"
    assert [method for method, _ in provider.calls] == ["extract"]
    provider.assert_exhausted()


async def test_reject_at_a_review_with_a_policy_question_keeps_the_reject_prompt(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction(intent="reject", has_question=True)),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(sid, turn_request("No, can I still return it?", revision=1))

    assert response.reply == f"{REJECT_PROMPT} {POLICY_QUESTION_SUFFIX}"
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None
    stored = await repository.load(sid)
    assert stored.review_rejected is True
    provider.assert_exhausted()


async def test_confirmation_with_a_policy_question_redisplays_the_review_and_suffix(
    tmp_path,
):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", FULL_EXTRACTION),
            ("extract", make_extraction(intent="confirm", has_question=True)),
        ],
    )
    created = await engine.create_session()
    sid = created.session_id
    await engine.turn(sid, turn_request())

    response = await engine.turn(
        sid, turn_request("Yes, but can I return it?", revision=1)
    )

    assert response.reply == (
        SUMMARY
        + "\nPlease confirm these details or tell me what to change. "
        + POLICY_QUESTION_SUFFIX
    )
    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SUMMARY
    stored = await repository.load(sid)
    assert stored.status == "awaiting_confirmation"
    assert stored.confirmation is None
    provider.assert_exhausted()
