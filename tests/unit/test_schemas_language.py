"""Unit tests for the language fields added to the strict wire/session models."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import (
    Extraction,
    ExtractionContext,
    KnowledgeContext,
    Message,
    Session,
    Slots,
)

NO_SLOTS = {
    "order_number": None,
    "category": None,
    "description": None,
    "urgency": None,
}
NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def extraction_payload(**overrides):
    payload = {
        "intent": "provide_details",
        "has_question": False,
        "candidates": dict(NO_SLOTS),
        "explicit_corrections": [],
        "ambiguous_fields": [],
        "detected_language": "en",
        "requested_language": None,
        "sentiment": "neutral",
        "frustration": "none",
    }
    payload.update(overrides)
    return payload


def test_extraction_requires_both_language_fields():
    payload = extraction_payload()
    del payload["detected_language"]
    with pytest.raises(ValidationError):
        Extraction.model_validate(payload)

    payload = extraction_payload()
    del payload["requested_language"]
    with pytest.raises(ValidationError):
        Extraction.model_validate(payload)


def test_extraction_accepts_en_es_and_unsupported_detection():
    for detected in ("en", "es", "unsupported"):
        extraction = Extraction.model_validate(
            extraction_payload(detected_language=detected)
        )
        assert extraction.detected_language == detected


def test_extraction_rejects_any_other_detected_language():
    for detected in ("fr", "EN", "", None):
        with pytest.raises(ValidationError):
            Extraction.model_validate(extraction_payload(detected_language=detected))


def test_extraction_accepts_only_supported_requested_languages_or_null():
    for requested in ("en", "es", None):
        extraction = Extraction.model_validate(
            extraction_payload(requested_language=requested)
        )
        assert extraction.requested_language == requested
    for requested in ("fr", "EN", ""):
        with pytest.raises(ValidationError):
            Extraction.model_validate(extraction_payload(requested_language=requested))


def test_extraction_context_defaults_to_english_reply_language():
    context = ExtractionContext(
        slots=Slots(**NO_SLOTS),
        pending_clarifications={},
        recent_messages=[],
        current_message="hello",
        status="collecting",
        frustration_active=False,
    )

    assert context.reply_language == "en"


def test_extraction_context_accepts_spanish_reply_language():
    context = ExtractionContext(
        slots=Slots(**NO_SLOTS),
        pending_clarifications={},
        recent_messages=[],
        current_message="hola",
        status="collecting",
        reply_language="es",
        frustration_active=False,
    )

    assert context.reply_language == "es"


def test_knowledge_context_defaults_to_english_and_accepts_spanish():
    base = {"question": "¿Cuánto tarda la entrega?", "kb_version": "2026-10-22", "sources": []}
    assert KnowledgeContext(**base).reply_language == "en"
    assert KnowledgeContext(**base, reply_language="es").reply_language == "es"
    with pytest.raises(ValidationError):
        KnowledgeContext(**base, reply_language="fr")


def test_message_language_defaults_to_english_for_version_one_files():
    message = Message(
        id=uuid4(),
        role="assistant",
        text="Hello",
        input_mode="system",
        created_at=NOW,
        sentiment=None,
        frustration=None,
    )

    assert message.language == "en"


def test_message_language_must_be_en_or_es():
    Message(
        id=uuid4(),
        role="assistant",
        text="Hola",
        input_mode="system",
        created_at=NOW,
        language="es",
        sentiment=None,
        frustration=None,
    )
    with pytest.raises(ValidationError):
        Message(
            id=uuid4(),
            role="assistant",
            text="Bonjour",
            input_mode="system",
            created_at=NOW,
            language="fr",
            sentiment=None,
            frustration=None,
        )


def test_session_reply_language_defaults_to_english_and_accepts_spanish():
    base = {
        "schema_version": 2,
        "session_id": uuid4(),
        "revision": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "status": "collecting",
        "frustration_active": False,
        "slots": Slots(**NO_SLOTS),
        "messages": [],
        "pending_clarifications": {},
        "pending_review_revision": None,
        "review_rejected": False,
        "collection_turn_count": 0,
        "summary": None,
        "confirmation": None,
        "requests": [],
    }

    assert Session(**base).reply_language == "en"
    assert Session(**base, reply_language="es").reply_language == "es"
    with pytest.raises(ValidationError):
        Session(**base, reply_language="fr")
