"""Unit tests for the conversation turn transaction (IC §3-§7, RAG §5-§6): intents."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.conversation import ConversationEngine
from app.prompts import (
    ABSTENTION,
    CAPABILITY,
    CAPABILITY_REPLY,
    GRATITUDE_ACKNOWLEDGMENT,
    INVALID_ORDER,
    MISSING_CATEGORY,
    MISSING_ORDER,
    NO_REVIEW_TO_REJECT,
    NOTHING_TO_CONFIRM,
    POLICY_QUESTION_SUFFIX,
    REJECT_PROMPT,
    SOCIAL_ACKNOWLEDGMENT,
)
from app.schemas import KnowledgeAnswer
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
    rejection,
    turn_request,
)


def social_extraction(has_question: bool = False):
    return make_extraction(intent="social", has_question=has_question)


def capability_extraction(has_question: bool = False):
    return make_extraction(intent="capability", has_question=has_question)


def start_intake_extraction(has_question: bool = False):
    return make_extraction(intent="start_intake", has_question=has_question)


async def test_pure_social_replies_with_the_exact_acknowledgment_only(tmp_path):
    for message, has_question in (
        ("Hello", False),
        ("Hello, how are you?", True),
        ("How is your day?", True),
    ):
        engine, repository, provider = build_engine(
            tmp_path, script=[("extract", social_extraction(has_question))]
        )
        created = await engine.create_session()

        response = await engine.turn(created.session_id, turn_request(message))

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
        assert stored.messages[-2].text == message
        assert stored.messages[-1].text == SOCIAL_ACKNOWLEDGMENT
        assert stored.messages[-1].citations == []
        provider.assert_exhausted()


async def test_gratitude_gets_a_thank_you_reply_without_changing_intake(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", make_extraction(intent="gratitude"))]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Thanks."))

    assert response.reply == GRATITUDE_ACKNOWLEDGMENT
    assert response.citations == []
    assert response.slots == make_slots()
    assert response.status == "collecting"
    assert [method for method, _ in provider.calls] == ["extract"]
    stored = await repository.load(created.session_id)
    assert stored.messages[-1].text == response.reply


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
