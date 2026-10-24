"""Unit tests: one acknowledgment per frustration episode (ORB-42, FEAT-01).

The pure transition/composition seams are exercised first, then the real engine
transaction: label persistence, flag publication, replay, restart, rollback and
per-session isolation. Only the remote AI provider and the knowledge lookup are
scripted boundaries; the repository, engine and schemas are real.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from app.conversation import (
    ConversationEngine,
    compose_frustration_reply,
    transition_frustration,
)
from app.errors import AppError, app_error
from app.localization import text
from app.prompts import MISSING_CATEGORY, SOCIAL_ACKNOWLEDGMENT
from app.schemas import ConfirmRequest, KnowledgeAnswer, TurnRequest
from app.storage import JsonSessionRepository
from tests.support.unit_fakes import (
    FakeKnowledgeBase,
    FakeProvider,
    make_extraction,
    make_record,
    make_session,
    make_slots,
)

EN_PREFIX = "I understand this is frustrating."
ES_PREFIX = "Entiendo que esto es frustrante."
ACK_EN = text("frustration_acknowledgment", "en")
ACK_ES = text("frustration_acknowledgment", "es")

FULL_SLOTS = make_slots(
    order_number="NS-000123",
    category="damaged_item",
    description="My order arrived broken",
    urgency="high",
)

TRANSITION_ROWS = [
    (False, "none", False, False),
    (False, "uncertain", False, False),
    (False, "present", True, True),
    (True, "none", False, False),
    (True, "uncertain", True, False),
    (True, "present", True, False),
]


def build_engine(tmp_path, script=(), kb_answers=None):
    repository = JsonSessionRepository(tmp_path)
    provider = FakeProvider(script)
    knowledge_base = FakeKnowledgeBase(kb_answers or {})
    return (
        ConversationEngine(repository, provider, knowledge_base),
        repository,
        provider,
    )


def turn_request(text_value="My order arrived broken and I am frustrated.", revision=0, request_id=None):
    return TurnRequest(
        request_id=request_id or uuid4(),
        expected_revision=revision,
        text=text_value,
        input_mode="text",
    )


class FailingExtractionProvider(FakeProvider):
    """Provider whose extraction fails safely (external service boundary)."""

    async def extract(self, context: object):
        self.calls.append(("extract", context))
        raise app_error("invalid_provider_response")


class FailingAnswerProvider(FakeProvider):
    """Provider whose grounded answer fails after a valid extraction."""

    async def answer_knowledge(self, context: object):
        self.calls.append(("answer_knowledge", context))
        raise app_error("provider_unavailable")


# --------------------------------------------------------------------------- #
# Pure transition and composition seams (FR02)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "previous_active,label,next_active,acknowledge", TRANSITION_ROWS
)
def test_transition_table_returns_the_exact_row(
    previous_active, label, next_active, acknowledge
):
    assert transition_frustration(previous_active, label) == (
        next_active,
        acknowledge,
    )


def test_transition_is_independent_of_sentiment():
    assert transition_frustration(False, "present") == (True, True)
    assert transition_frustration(True, "present") == (True, False)


def test_compose_returns_the_base_reply_unchanged_without_acknowledgment():
    assert compose_frustration_reply("Normal reply.", False, "en") == "Normal reply."
    assert compose_frustration_reply("Respuesta normal.", False, "es") == "Respuesta normal."


def test_compose_prefixes_the_exact_english_copy_with_two_newlines():
    assert compose_frustration_reply("Normal reply.", True, "en") == (
        f"{EN_PREFIX}\n\nNormal reply."
    )


def test_compose_prefixes_the_exact_spanish_copy_with_two_newlines():
    assert compose_frustration_reply("Respuesta normal.", True, "es") == (
        f"{ES_PREFIX}\n\nRespuesta normal."
    )


# --------------------------------------------------------------------------- #
# Engine episode behavior
# --------------------------------------------------------------------------- #


async def test_new_session_has_no_episode_and_no_acknowledgment(tmp_path):
    engine, repository, provider = build_engine(tmp_path)

    view = await engine.create_session()

    stored = await repository.load(view.session_id)
    assert view.frustration_active is False
    assert stored.frustration_active is False
    assert stored.messages[0].sentiment is None
    assert stored.messages[0].frustration is None
    provider.assert_exhausted()


async def test_first_present_turn_acknowledges_once_and_sets_the_flag(tmp_path):
    extraction = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("I have asked three times. This is really frustrating.", revision=0),
    )

    stored = await repository.load(created.session_id)
    assert response.frustration_active is True
    assert stored.frustration_active is True
    assert response.reply == f"{EN_PREFIX}\n\n{MISSING_CATEGORY}"
    assert stored.messages[-2].frustration == "present"
    assert stored.messages[-2].sentiment == "negative"
    assert stored.messages[-1].frustration is None
    provider.assert_exhausted()


async def test_present_present_acknowledges_only_the_first_turn(tmp_path):
    first = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    second = make_extraction(
        candidates={"category": "damaged_item"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", first), ("extract", second)]
    )
    created = await engine.create_session()

    first_response = await engine.turn(
        created.session_id, turn_request("I am frustrated.", revision=0)
    )
    second_response = await engine.turn(
        created.session_id,
        turn_request("Still frustrated. It was damaged.", revision=1),
    )

    assert first_response.reply.startswith(EN_PREFIX)
    assert not second_response.reply.startswith(EN_PREFIX)
    assert first_response.frustration_active is True
    assert second_response.frustration_active is True
    stored = await repository.load(created.session_id)
    assert stored.frustration_active is True
    assert [message.frustration for message in stored.messages[1::2]] == [
        "present",
        "present",
    ]
    provider.assert_exhausted()


async def test_present_none_present_starts_two_episodes(tmp_path):
    script = [
        (
            "extract",
            make_extraction(
                candidates={"order_number": "NS-000123"},
                sentiment="negative",
                frustration="present",
            ),
        ),
        (
            "extract",
            make_extraction(intent="question", sentiment="neutral", frustration="none"),
        ),
        (
            "extract",
            make_extraction(
                candidates={"description": "Still no update after three days."},
                sentiment="negative",
                frustration="present",
            ),
        ),
    ]
    engine, repository, provider = build_engine(tmp_path, script=script)
    created = await engine.create_session()

    first = await engine.turn(created.session_id, turn_request("I am frustrated.", revision=0))
    second = await engine.turn(
        created.session_id,
        turn_request("How long does delivery take?", revision=1),
    )
    third = await engine.turn(
        created.session_id,
        turn_request("Still no update after three days.", revision=2),
    )

    assert first.reply.startswith(EN_PREFIX)
    assert first.frustration_active is True
    assert second.frustration_active is False
    assert not second.reply.startswith(EN_PREFIX)
    assert third.frustration_active is True
    assert third.reply.startswith(EN_PREFIX)
    stored = await repository.load(created.session_id)
    assert stored.frustration_active is True
    assert sum(
        message.text.startswith(EN_PREFIX)
        for message in stored.messages
        if message.role == "assistant"
    ) == 2
    provider.assert_exhausted()


async def test_present_uncertain_present_does_not_repeat_the_acknowledgment(tmp_path):
    script = [
        (
            "extract",
            make_extraction(
                candidates={"order_number": "NS-000123"},
                sentiment="negative",
                frustration="present",
            ),
        ),
        (
            "extract",
            make_extraction(
                intent="unrelated", sentiment="uncertain", frustration="uncertain"
            ),
        ),
        (
            "extract",
            make_extraction(
                candidates={"category": "damaged_item"},
                sentiment="negative",
                frustration="present",
            ),
        ),
    ]
    engine, repository, provider = build_engine(tmp_path, script=script)
    created = await engine.create_session()

    first = await engine.turn(created.session_id, turn_request("I am frustrated.", revision=0))
    second = await engine.turn(created.session_id, turn_request("Hmm, not sure.", revision=1))
    third = await engine.turn(
        created.session_id, turn_request("It was damaged.", revision=2)
    )

    assert first.reply.startswith(EN_PREFIX)
    assert second.frustration_active is True
    assert not second.reply.startswith(EN_PREFIX)
    assert third.frustration_active is True
    assert not third.reply.startswith(EN_PREFIX)
    stored = await repository.load(created.session_id)
    assert sum(
        message.text.startswith(EN_PREFIX)
        for message in stored.messages
        if message.role == "assistant"
    ) == 1
    provider.assert_exhausted()


async def test_negative_sentiment_with_none_frustration_gets_no_prefix_or_urgency(tmp_path):
    extraction = make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My item is broken.",
        },
        sentiment="negative",
        frustration="none",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id, turn_request("My item is broken.", revision=0)
    )

    assert response.frustration_active is False
    assert not response.reply.startswith(EN_PREFIX)
    assert response.slots.urgency is None
    stored = await repository.load(created.session_id)
    assert stored.frustration_active is False
    assert stored.slots.urgency is None
    provider.assert_exhausted()


async def test_uncertain_label_is_valid_and_does_not_start_an_episode(tmp_path):
    extraction = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="uncertain",
        frustration="uncertain",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(created.session_id, turn_request("Great. Just great.", revision=0))

    assert response.frustration_active is False
    assert not response.reply.startswith(EN_PREFIX)
    stored = await repository.load(created.session_id)
    assert stored.messages[-2].sentiment == "uncertain"
    assert stored.messages[-2].frustration == "uncertain"
    provider.assert_exhausted()


async def test_spanish_episode_start_uses_the_exact_spanish_prefix(tmp_path):
    extraction = make_extraction(
        detected_language="es",
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("Ya he preguntado tres veces y estoy muy frustrado.", revision=0),
    )

    assert response.reply_language == "es"
    assert response.frustration_active is True
    assert response.reply == f"{ES_PREFIX}\n\n{text('intake.category', 'es')}"
    assert EN_PREFIX not in response.reply
    provider.assert_exhausted()


async def test_frustrated_policy_question_prefixes_only_the_cited_answer(tmp_path):
    record = make_record()
    answer = KnowledgeAnswer(
        answerable=True,
        answer="Standard delivery takes three to five business days.",
        cited_ids=[record.id],
    )
    extraction = make_extraction(
        intent="question",
        has_question=True,
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", extraction), ("answer_knowledge", answer)],
        kb_answers={"How long does delivery take? I am frustrated.": [record]},
    )
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("How long does delivery take? I am frustrated.", revision=0),
    )

    assert response.reply == (
        f"{EN_PREFIX}\n\nStandard delivery takes three to five business days."
    )
    assert [citation.source_id for citation in response.citations] == [record.id]
    assert response.slots == make_slots()
    assert response.pending_review_revision is None
    assert "order number" not in response.reply
    provider.assert_exhausted()


async def test_spanish_policy_question_suffix_uses_the_turn_language(tmp_path):
    extraction = make_extraction(
        detected_language="es",
        has_question=True,
        candidates={"order_number": "NS-000123"},
        sentiment="neutral",
        frustration="none",
    )
    engine, _, provider = build_engine(tmp_path, script=[("extract", extraction)])
    created = await engine.create_session()

    response = await engine.turn(
        created.session_id,
        turn_request("Mi pedido NS-000123. ¿Cuánto tarda la entrega?", revision=0),
    )

    assert response.reply_language == "es"
    assert response.reply == (
        f"{text('intake.category', 'es')} "
        f"{text('policy_question_suffix', 'es')}"
    )
    assert text("policy_question_suffix", "en") not in response.reply
    provider.assert_exhausted()


async def test_social_turn_with_none_resets_an_active_episode_without_acknowledgment(tmp_path):
    script = [
        (
            "extract",
            make_extraction(
                candidates={"order_number": "NS-000123"},
                sentiment="negative",
                frustration="present",
            ),
        ),
        (
            "extract",
            make_extraction(intent="social", sentiment="positive", frustration="none"),
        ),
    ]
    engine, repository, provider = build_engine(tmp_path, script=script)
    created = await engine.create_session()
    await engine.turn(created.session_id, turn_request("I am frustrated.", revision=0))

    response = await engine.turn(
        created.session_id, turn_request("Thanks, that helps.", revision=1)
    )

    assert response.frustration_active is False
    assert response.reply == SOCIAL_ACKNOWLEDGMENT
    stored = await repository.load(created.session_id)
    assert stored.frustration_active is False
    provider.assert_exhausted()


async def test_failed_extraction_saves_nothing(tmp_path):
    engine, repository, _ = build_engine(tmp_path)
    provider = FailingExtractionProvider()
    engine.provider = provider
    created = await engine.create_session()
    path = tmp_path / f"{created.session_id}.json"
    original = path.read_bytes()

    with pytest.raises(AppError) as excinfo:
        await engine.turn(created.session_id, turn_request(revision=0))

    assert excinfo.value.code == "invalid_provider_response"
    assert path.read_bytes() == original
    stored = await repository.load(created.session_id)
    assert stored.revision == 0
    assert stored.frustration_active is False
    assert len(stored.messages) == 1
    assert stored.requests == []


async def test_failed_rag_after_present_extraction_publishes_no_flag(tmp_path):
    record = make_record()
    extraction = make_extraction(
        intent="question",
        has_question=True,
        sentiment="negative",
        frustration="present",
    )
    repository = JsonSessionRepository(tmp_path)
    provider = FailingAnswerProvider([("extract", extraction)])
    engine = ConversationEngine(
        repository,
        provider,
        FakeKnowledgeBase({"How long does delivery take?": [record]}),
    )
    created = await engine.create_session()
    path = tmp_path / f"{created.session_id}.json"
    original = path.read_bytes()

    with pytest.raises(AppError) as excinfo:
        await engine.turn(
            created.session_id, turn_request("How long does delivery take?", revision=0)
        )

    assert excinfo.value.code == "provider_unavailable"
    assert path.read_bytes() == original
    stored = await repository.load(created.session_id)
    assert stored.revision == 0
    assert stored.frustration_active is False
    assert len(stored.messages) == 1
    assert stored.requests == []


async def test_failed_atomic_save_keeps_prior_bytes_and_episode(tmp_path, monkeypatch):
    import app.storage as storage_module

    extraction = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()
    path = tmp_path / f"{created.session_id}.json"
    original = path.read_bytes()

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(AppError) as excinfo:
        await engine.turn(created.session_id, turn_request(revision=0))

    assert excinfo.value.code == "storage_unavailable"
    assert path.read_bytes() == original
    stored = await repository.load(created.session_id)
    assert stored.revision == 0
    assert stored.frustration_active is False
    assert stored.requests == []
    provider.assert_exhausted()


async def test_exact_retry_returns_the_historical_response_without_another_prefix(tmp_path):
    extraction = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    created = await engine.create_session()
    command = turn_request(revision=0)

    first = await engine.turn(created.session_id, command)
    replay = await engine.turn(created.session_id, command)

    assert replay == first
    assert replay.reply.startswith(EN_PREFIX)
    assert replay.reply.count(EN_PREFIX) == 1
    stored = await repository.load(created.session_id)
    assert stored.revision == 1
    assert len(stored.messages) == 3
    assert len(stored.requests) == 1
    provider.assert_exhausted()


async def test_restart_during_an_active_episode_preserves_suppression(tmp_path):
    first_extraction = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", first_extraction)]
    )
    created = await engine.create_session()
    first = await engine.turn(created.session_id, turn_request(revision=0))
    assert first.reply.startswith(EN_PREFIX)

    # A new engine instance over the same directory is a process restart.
    second_extraction = make_extraction(
        candidates={"category": "damaged_item"},
        sentiment="negative",
        frustration="present",
    )
    restarted_provider = FakeProvider([("extract", second_extraction)])
    restarted = ConversationEngine(
        JsonSessionRepository(tmp_path), restarted_provider, FakeKnowledgeBase()
    )

    second = await restarted.turn(
        created.session_id, turn_request("Still frustrated. It was damaged.", revision=1)
    )

    assert second.frustration_active is True
    assert not second.reply.startswith(EN_PREFIX)
    assert restarted_provider.calls[0][1].frustration_active is True
    restarted_provider.assert_exhausted()
    provider.assert_exhausted()


async def test_separate_sessions_keep_independent_episodes(tmp_path):
    first_extraction = make_extraction(
        candidates={"order_number": "NS-000123"},
        sentiment="negative",
        frustration="present",
    )
    second_extraction = make_extraction(
        candidates={"order_number": "NS-000456"},
        sentiment="negative",
        frustration="present",
    )
    engine, repository, provider = build_engine(
        tmp_path,
        script=[("extract", first_extraction), ("extract", second_extraction)],
    )
    first_session = await engine.create_session()
    second_session = await engine.create_session()

    first = await engine.turn(first_session.session_id, turn_request(revision=0))
    second = await engine.turn(second_session.session_id, turn_request(revision=0))

    assert first.reply.startswith(EN_PREFIX)
    assert second.reply.startswith(EN_PREFIX)
    first_stored = await repository.load(first_session.session_id)
    second_stored = await repository.load(second_session.session_id)
    assert first_stored.frustration_active is True
    assert second_stored.frustration_active is True
    assert first_stored.messages[-2].id != second_stored.messages[-2].id
    assert first_stored.session_id != second_stored.session_id
    provider.assert_exhausted()


async def test_dedicated_confirmation_and_get_do_not_classify_or_transition(tmp_path):
    session = make_session(
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        pending_review_revision=1,
        summary="Order: NS-000123",
        frustration_active=True,
    )
    engine, repository, provider = build_engine(tmp_path)
    await repository.save(session)

    view = await engine.get_session(session.session_id)
    response = await engine.confirm(
        session.session_id,
        ConfirmRequest(request_id=uuid4(), expected_revision=0, review_revision=1),
    )

    assert view.frustration_active is True
    assert response.frustration_active is True
    stored = await repository.load(session.session_id)
    assert stored.frustration_active is True
    assert stored.messages[-2].input_mode == "system"
    assert stored.messages[-2].sentiment is None
    assert stored.messages[-2].frustration is None
    assert stored.messages[-1].sentiment is None
    assert stored.messages[-1].frustration is None
    provider.assert_exhausted()


async def test_unsupported_language_retains_the_episode_and_stores_uncertain_labels(tmp_path):
    script = [
        (
            "extract",
            make_extraction(
                candidates={"order_number": "NS-000123"},
                sentiment="negative",
                frustration="present",
            ),
        ),
        (
            "extract",
            make_extraction(
                detected_language="unsupported",
                sentiment="negative",
                frustration="present",
            ),
        ),
    ]
    engine, repository, provider = build_engine(tmp_path, script=script)
    created = await engine.create_session()
    await engine.turn(created.session_id, turn_request("I am frustrated.", revision=0))

    response = await engine.turn(
        created.session_id,
        turn_request("Je suis très frustré.", revision=1),
    )

    assert response.frustration_active is True
    assert response.reply == text("unsupported_language", "en")
    assert not response.reply.startswith(EN_PREFIX)
    stored = await repository.load(created.session_id)
    assert stored.frustration_active is True
    assert stored.messages[-2].sentiment == "uncertain"
    assert stored.messages[-2].frustration == "uncertain"
    provider.assert_exhausted()


async def test_natural_language_confirmation_with_present_prefixes_the_receipt_once(tmp_path):
    session = make_session(
        status="awaiting_confirmation",
        slots=FULL_SLOTS,
        pending_review_revision=1,
        summary="Order: NS-000123",
    )
    extraction = make_extraction(intent="confirm", sentiment="negative", frustration="present")
    engine, repository, provider = build_engine(
        tmp_path, script=[("extract", extraction)]
    )
    await repository.save(session)
    command = turn_request("Yes, confirm it. I am still frustrated.", revision=0)

    response = await engine.turn(session.session_id, command)
    replay = await engine.turn(session.session_id, command)

    receipt = text("receipt", "en").format(session_id=session.session_id)
    assert response.status == "completed"
    assert response.frustration_active is True
    assert response.reply == f"{EN_PREFIX}\n\n{receipt}"
    assert replay == response
    stored = await repository.load(session.session_id)
    assert stored.status == "completed"
    assert stored.frustration_active is True
    assert stored.messages[-2].frustration == "present"
    assert stored.messages[-1].frustration is None
    assert stored.requests[0].response.reply.count(EN_PREFIX) == 1
    provider.assert_exhausted()
