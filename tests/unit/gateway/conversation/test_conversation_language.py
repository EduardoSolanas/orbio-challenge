"""Unit tests for English/Spanish conversation behaviour.

Every test drives the real engine and real JSON repository; only the remote AI
provider and the knowledge-base lookup are scripted doubles.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from gateway.conversation.engine import ConversationEngine
from gateway.errors import AppError
from gateway.localization import (
    build_review_reply,
    build_summary,
    render,
    request_reference,
    text,
)
from gateway.schemas import (
    Citation,
    ConfirmRequest,
    KnowledgeAnswer,
    TurnRequest,
)
from gateway.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_record,
    make_session,
    make_slots,
)

FULL_SLOTS = make_slots(
    order_number="NS-000123",
    category="damaged_item",
    description="Mi pedido llegó dañado",
    urgency="high",
)
SPANISH_FULL_EXTRACTION = make_extraction(
    intent="provide_details",
    detected_language="es",
    candidates={
        "order_number": "NS-000123",
        "category": "artículo dañado",
        "description": "Mi pedido llegó dañado",
        "urgency": "alta",
    },
)
SPANISH_SUMMARY = build_summary(FULL_SLOTS, "es")
ENGLISH_SUMMARY = build_summary(FULL_SLOTS, "en")


def build_engine(tmp_path, script=(), kb_answers=None, speech_languages=("en",)):
    repository = JsonSessionRepository(tmp_path)
    provider = FakeProvider(script)
    knowledge_base = FakeKnowledgeBase(kb_answers)
    engine = ConversationEngine(
        repository,
        provider,
        knowledge_base,
        speech_languages=frozenset(speech_languages),
    )
    return engine, repository, provider


def turn_request(text_value="NS-000123 arrived broken", revision=0, input_mode="text", request_id=None):
    return TurnRequest(
        request_id=request_id or uuid4(),
        expected_revision=revision,
        text=text_value,
        input_mode=input_mode,
    )


async def seed_review_session(repository, *, reply_language="en"):
    session = make_session(
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        pending_review_revision=1,
        summary=build_summary(FULL_SLOTS, reply_language),
        reply_language=reply_language,
        revision=1,
    )
    await repository.save(session)
    return session


async def test_new_session_starts_in_english_with_a_playable_greeting(tmp_path):
    engine, _, _ = build_engine(tmp_path)

    view = await engine.create_session()

    assert view.reply_language == "en"
    assert view.messages[0].language == "en"
    assert view.messages[0].text == text("greeting", "en")
    assert view.messages[0].speech_available is True


async def test_spanish_turn_switches_reply_language_and_keeps_canonical_values(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", SPANISH_FULL_EXTRACTION)]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("Mi pedido NS-000123 llegó dañado, urgencia alta"),
    )

    assert response.reply_language == "es"
    assert response.slots.order_number == "NS-000123"
    assert response.slots.category == "damaged_item"
    assert response.slots.description == "Mi pedido llegó dañado"
    assert response.slots.urgency == "high"
    assert response.status == "awaiting_confirmation"
    assert response.summary == SPANISH_SUMMARY
    assert response.reply == build_review_reply(FULL_SLOTS, "es")

    stored = await repository.load(created.session_id)
    assert stored.reply_language == "es"
    assert stored.slots == FULL_SLOTS
    assert stored.messages[-2].language == "es"
    assert stored.messages[-1].language == "es"
    provider.assert_exhausted()


async def test_extraction_context_carries_the_current_reply_language(tmp_path):
    engine, _, provider = build_engine(
        tmp_path, script=[("extract", SPANISH_FULL_EXTRACTION)]
    )
    created = await engine.create_session()

    await engine.turn(created.session_id, turn_request("hola"))

    method, context = provider.calls[-1]
    assert method == "extract"
    assert context.reply_language == "en"


async def test_explicit_switch_to_spanish_redisplays_the_review_without_confirming(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="unrelated",
                    detected_language="en",
                    requested_language="es",
                ),
            )
        ],
    )
    session = await seed_review_session(repository)

    response = await engine.turn(
        session.session_id, turn_request("Please continue in Spanish", revision=1)
    )

    assert response.reply_language == "es"
    assert response.status == "awaiting_confirmation"
    assert response.reply == build_review_reply(FULL_SLOTS, "es")
    assert response.summary == SPANISH_SUMMARY
    assert response.pending_review_revision == session.pending_review_revision
    assert response.slots == session.slots

    stored = await repository.load(session.session_id)
    assert stored.status == "awaiting_confirmation"
    assert stored.confirmation is None
    assert stored.reply_language == "es"
    assert stored.pending_review_revision == 1
    provider.assert_exhausted()


async def test_explicit_switch_back_to_english_localizes_the_next_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="unrelated",
                    detected_language="es",
                    requested_language="en",
                ),
            )
        ],
    )
    session = await seed_review_session(repository, reply_language="es")

    response = await engine.turn(
        session.session_id, turn_request("Continuemos en inglés", revision=1)
    )

    assert response.reply_language == "en"
    assert response.reply == build_review_reply(FULL_SLOTS, "en")
    assert response.summary == ENGLISH_SUMMARY
    assert response.slots == FULL_SLOTS


async def test_unsupported_language_returns_a_localized_notice_and_accepts_no_facts(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="provide_details",
                    detected_language="unsupported",
                    candidates={
                        "order_number": "NS-999999",
                        "category": "damaged_item",
                        "description": "Bonjour mon colis est cassé",
                        "urgency": "high",
                    },
                ),
            )
        ],
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Bonjour, mon colis est cassé")
    )

    assert response.reply == text("unsupported_language", "en")
    assert response.reply_language == "en"
    assert response.slots == make_slots()
    assert response.status == "collecting"
    assert response.pending_review_revision is None
    assert response.summary is None

    stored = await repository.load(created.session_id)
    assert stored.slots == make_slots()
    assert stored.collection_turn_count == 1
    assert stored.messages[-2].language == "en"
    provider.assert_exhausted()


async def test_unsupported_language_notice_is_spanish_in_a_spanish_session(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(detected_language="unsupported", intent="unrelated"),
            )
        ],
    )
    session = await seed_review_session(repository, reply_language="es")

    response = await engine.turn(
        session.session_id, turn_request("Bonjour", revision=1)
    )

    assert response.reply == text("unsupported_language", "es")
    assert response.reply_language == "es"
    assert response.status == "awaiting_confirmation"
    assert response.pending_review_revision == 1
    assert response.summary == SPANISH_SUMMARY


async def test_unsupported_input_with_an_explicit_switch_applies_only_the_language(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="provide_details",
                    detected_language="unsupported",
                    requested_language="es",
                    candidates={
                        "order_number": "NS-999999",
                        "category": "damaged_item",
                        "description": "Bonjour mon colis est cassé",
                        "urgency": "high",
                    },
                ),
            )
        ],
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Switch to Spanish: bonjour mon colis")
    )

    assert response.reply_language == "es"
    assert response.reply == text("language_switch_restate", "es")
    assert response.slots == make_slots()
    assert response.status == "collecting"

    stored = await repository.load(created.session_id)
    assert stored.reply_language == "es"
    assert stored.slots == make_slots()


async def test_mixed_language_correction_preserves_canonical_values_and_switches_language(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="correction",
                    detected_language="es",
                    candidates={"category": "artículo incorrecto", "urgency": "alta"},
                    explicit_corrections=["category", "urgency"],
                ),
            )
        ],
    )
    session = make_session(
        slots=make_slots(
            order_number="NS-000123",
            category="damaged_item",
            description="My order arrived broken",
            urgency="low",
        ),
        revision=1,
    )
    await repository.save(session)

    response = await engine.turn(
        session.session_id,
        turn_request("Sorry, actually it was the wrong item and it is urgent", revision=1),
    )

    assert response.reply_language == "es"
    assert response.slots.order_number == "NS-000123"
    assert response.slots.category == "wrong_item"
    assert response.slots.description == "My order arrived broken"
    assert response.slots.urgency == "high"
    assert response.reply == build_review_reply(response.slots, "es")


async def test_numeric_only_turn_keeps_the_current_reply_language(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    detected_language="es",
                    candidates={"order_number": "000123"},
                ),
            )
        ],
    )
    session = make_session(reply_language="es", revision=1)
    await repository.save(session)

    response = await engine.turn(session.session_id, turn_request("000123", revision=1))

    assert response.reply_language == "es"
    assert response.slots.order_number == "NS-000123"
    assert response.reply == text("intake.category", "es")


async def test_spanish_policy_question_gets_a_spanish_answer_with_canonical_citations(tmp_path):
    record = make_record(
        record_id="faq-delivery",
        title="Delivery times",
        keywords=["delivery", "shipping"],
        body="Standard delivery takes 2 to 5 business days after dispatch.",
    )
    answer = KnowledgeAnswer(
        answerable=True,
        answer="La entrega estándar tarda de 2 a 5 días laborables.",
        cited_ids=["faq-delivery"],
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True, detected_language="es")),
            ("answer_knowledge", answer),
        ],
        kb_answers={"¿Cuánto tarda la entrega?": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("¿Cuánto tarda la entrega?")
    )

    assert response.reply == answer.answer
    assert response.reply_language == "es"
    assert response.citations == [
        Citation(
            source_id="faq-delivery",
            title="Delivery times",
            excerpt=record.body,
            kb_version="2026-10-22",
        )
    ]
    method, context = provider.calls[-1]
    assert method == "answer_knowledge"
    assert context.reply_language == "es"
    assert context.question == "¿Cuánto tarda la entrega?"


async def test_spanish_abstention_is_localized_and_keeps_no_citations(tmp_path):
    engine, _, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(intent="question", has_question=True, detected_language="es")),
        ],
        kb_answers={"¿Puedo pagar con tarjeta?": []},
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("¿Puedo pagar con tarjeta?"))

    assert response.reply == text("abstention", "es")
    assert response.reply_language == "es"
    assert response.citations == []


async def test_replay_returns_the_stored_response_with_its_original_language(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(candidates={"order_number": "NS-000123"}, detected_language="en")),
            (
                "extract",
                make_extraction(
                    intent="unrelated", detected_language="en", requested_language="es"
                ),
            ),
        ],
    )
    created = await engine.create_session()
    first = turn_request("NS-000123")

    english_response = await engine.turn(created.session_id, first)
    await engine.turn(
        created.session_id, turn_request("Switch to Spanish", revision=1)
    )
    replay = await engine.turn(created.session_id, first)

    assert english_response.reply_language == "en"
    assert replay == english_response
    assert replay.reply_language == "en"
    stored = await repository.load(created.session_id)
    assert stored.reply_language == "es"
    assert len(stored.messages) == 5
    provider.assert_exhausted()


async def test_confirmation_receipt_is_localized_in_the_session_language(tmp_path):
    engine, repository, _ = build_engine(tmp_path)
    session = await seed_review_session(repository, reply_language="es")

    response = await engine.confirm(
        session.session_id,
        ConfirmRequest(
            request_id=uuid4(), expected_revision=1, review_revision=1
        ),
    )

    assert response.reply == text("receipt", "es").format(reference=request_reference(session.session_id))
    assert response.reply_language == "es"

    stored = await repository.load(session.session_id)
    assert stored.messages[-1].language == "es"
    assert stored.messages[-2].language == "es"


async def test_completed_spanish_session_stays_immutable(tmp_path):
    engine, repository, _ = build_engine(tmp_path)
    session = await seed_review_session(repository, reply_language="es")
    await engine.confirm(
        session.session_id,
        ConfirmRequest(request_id=uuid4(), expected_revision=1, review_revision=1),
    )

    with pytest.raises(AppError) as excinfo:
        await engine.turn(session.session_id, turn_request("hola", revision=2))

    assert excinfo.value.code == "session_completed"


async def test_transcript_turn_uses_the_same_language_and_intent_path(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", SPANISH_FULL_EXTRACTION)]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("Mi pedido llegó dañado", input_mode="transcript"),
    )

    assert response.reply_language == "es"
    stored = await repository.load(created.session_id)
    assert stored.messages[-2].input_mode == "transcript"
    assert stored.messages[-2].language == "es"


async def test_speech_availability_follows_each_saved_message_language(tmp_path):
    engine, repository, _ = build_engine(
        tmp_path,
        script=[
            ("extract", make_extraction(candidates={"order_number": "NS-000123"}, detected_language="en")),
            (
                "extract",
                make_extraction(
                    intent="unrelated", detected_language="en", requested_language="es"
                ),
            ),
        ],
        speech_languages=("en", "es"),
    )
    created = await engine.create_session()
    await engine.turn(created.session_id, turn_request("NS-000123"))
    await engine.turn(created.session_id, turn_request("En español, por favor", revision=1))

    view = await engine.get_session(created.session_id)

    assistant_messages = [message for message in view.messages if message.role == "assistant"]
    assert [message.language for message in assistant_messages] == ["en", "en", "es"]
    assert [message.speech_available for message in assistant_messages] == [True, True, True]


async def test_spanish_messages_report_no_speech_when_only_english_tts_is_configured(tmp_path):
    engine, repository, _ = build_engine(
        tmp_path,
        script=[("extract", SPANISH_FULL_EXTRACTION)],
        speech_languages=("en",),
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("Mi pedido llegó dañado")
    )

    assert response.reply_language == "es"
    view = await engine.get_session(created.session_id)
    assert view.messages[-1].language == "es"
    assert view.messages[-1].speech_available is False
    assert view.messages[0].speech_available is True


async def test_language_switch_preserves_a_rejected_review(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    intent="unrelated",
                    detected_language="en",
                    requested_language="es",
                ),
            )
        ],
    )
    session = make_session(
        status="collecting",
        slots=FULL_SLOTS,
        review_rejected=True,
        reply_language="en",
        revision=1,
    )
    await repository.save(session)

    response = await engine.turn(
        session.session_id, turn_request("Switch to Spanish", revision=1)
    )

    assert response.reply_language == "es"
    assert response.reply == text("reject_prompt", "es")
    stored = await repository.load(session.session_id)
    assert stored.review_rejected is True
    assert stored.reply_language == "es"


async def test_validation_message_is_localized_after_a_switch(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            (
                "extract",
                make_extraction(
                    detected_language="es",
                    candidates={"category": "paquete misterioso"},
                ),
            )
        ],
    )
    session = make_session(reply_language="es", revision=1)
    await repository.save(session)

    response = await engine.turn(
        session.session_id, turn_request("Es un paquete misterioso", revision=1)
    )

    assert response.reply == text("intake.invalid_category", "es")
    assert response.pending_clarifications["category"].code == "invalid"
    assert response.slots.category is None


async def test_resumed_session_keeps_the_persisted_reply_language(tmp_path):
    engine, repository, provider = build_engine(
        tmp_path,
        script=[
            ("extract", SPANISH_FULL_EXTRACTION),
            (
                "extract",
                make_extraction(detected_language="es", candidates={"urgency": "baja"}),
            ),
        ],
    )
    created = await engine.create_session()
    await engine.turn(created.session_id, turn_request("Mi pedido llegó dañado"))

    resumed = await engine.get_session(created.session_id)
    assert resumed.reply_language == "es"

    response = await engine.turn(
        created.session_id, turn_request("Mejor urgencia baja", revision=1)
    )

    assert response.reply_language == "es"
    assert response.reply == render(
        "intake.conflict",
        "es",
        {"old": "high", "candidate": "low", "field_label": "urgencia"},
    )
    assert response.slots.urgency == "high"
