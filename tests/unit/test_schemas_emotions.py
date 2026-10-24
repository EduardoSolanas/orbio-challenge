"""Strict label, flag and migration-shape tests for emotion metadata.

The contract requires exact lowercase enums on Extraction, nullable stored
labels on messages, and a strict boolean ``frustration_active`` on every stored
and served object. Missing or invalid labels never degrade to ``neutral``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas import (
    Extraction,
    ExtractionContext,
    Message,
    Session,
    SessionView,
    Slots,
    TurnResponse,
)

SESSION_ID = "3f7c1c1e-0f0a-4a4e-9a5e-2b1d0f6c8a11"
MESSAGE_ID = "b1e0f7a2-5c4d-4e6f-8a9b-0c1d2e3f4a5b"
CREATED_AT = "2026-10-22T12:00:00Z"

SENTIMENTS = ["positive", "neutral", "negative", "uncertain"]
FRUSTRATIONS = ["none", "present", "uncertain"]
INVALID_LABELS = [None, "", "Positive", " positive", "positive ", 1, True, []]


def slots_payload() -> dict:
    return {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }


def extraction_payload() -> dict:
    return {
        "intent": "provide_details",
        "has_question": False,
        "candidates": slots_payload(),
        "explicit_corrections": [],
        "ambiguous_fields": [],
        "detected_language": "en",
        "requested_language": None,
        "sentiment": "neutral",
        "frustration": "none",
    }


def message_payload(**overrides) -> dict:
    return {
        "id": MESSAGE_ID,
        "role": "user",
        "text": "My item is broken.",
        "input_mode": "text",
        "created_at": CREATED_AT,
        "citations": [],
        "sentiment": "negative",
        "frustration": "none",
        **overrides,
    }


def session_payload() -> dict:
    return {
        "schema_version": 2,
        "session_id": SESSION_ID,
        "revision": 0,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "status": "collecting",
        "slots": slots_payload(),
        "messages": [],
        "pending_clarifications": {},
        "pending_review_revision": None,
        "review_rejected": False,
        "collection_turn_count": 0,
        "summary": None,
        "confirmation": None,
        "requests": [],
        "frustration_active": False,
    }


def session_view_payload() -> dict:
    return {
        "session_id": SESSION_ID,
        "revision": 0,
        "status": "collecting",
        "slots": slots_payload(),
        "messages": [],
        "pending_clarifications": {},
        "pending_review_revision": None,
        "summary": None,
        "frustration_active": False,
    }


def turn_response_payload() -> dict:
    return {
        "session_id": SESSION_ID,
        "revision": 1,
        "status": "collecting",
        "reply": "Hello.",
        "message_id": MESSAGE_ID,
        "slots": slots_payload(),
        "pending_clarifications": {},
        "pending_review_revision": None,
        "summary": None,
        "citations": [],
        "frustration_active": False,
    }


def extraction_context_payload() -> dict:
    return {
        "slots": slots_payload(),
        "pending_clarifications": {},
        "recent_messages": [],
        "current_message": "My item is broken.",
        "status": "collecting",
        "intake_started": False,
        "frustration_active": False,
    }


@pytest.mark.parametrize("value", SENTIMENTS)
def test_extraction_accepts_every_sentiment_label(value):
    extraction = Extraction.model_validate({**extraction_payload(), "sentiment": value})

    assert extraction.sentiment == value


@pytest.mark.parametrize("value", FRUSTRATIONS)
def test_extraction_accepts_every_frustration_label(value):
    extraction = Extraction.model_validate(
        {**extraction_payload(), "frustration": value}
    )

    assert extraction.frustration == value


@pytest.mark.parametrize("value", INVALID_LABELS, ids=str)
def test_extraction_rejects_invalid_sentiment_labels(value):
    with pytest.raises(ValidationError):
        Extraction.model_validate({**extraction_payload(), "sentiment": value})


@pytest.mark.parametrize("value", INVALID_LABELS, ids=str)
def test_extraction_rejects_invalid_frustration_labels(value):
    with pytest.raises(ValidationError):
        Extraction.model_validate({**extraction_payload(), "frustration": value})


@pytest.mark.parametrize("field", ["sentiment", "frustration"])
def test_extraction_requires_both_emotion_labels(field):
    payload = extraction_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        Extraction.model_validate(payload)


@pytest.mark.parametrize("field", ["sentiment", "frustration"])
def test_message_requires_both_label_keys(field):
    payload = message_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        Message.model_validate(payload)


@pytest.mark.parametrize("role", ["user"])
@pytest.mark.parametrize("input_mode", ["text", "transcript"])
@pytest.mark.parametrize("sentiment", SENTIMENTS)
def test_customer_text_messages_accept_every_sentiment_label(
    role, input_mode, sentiment
):
    message = Message.model_validate(
        message_payload(role=role, input_mode=input_mode, sentiment=sentiment)
    )

    assert message.sentiment == sentiment


@pytest.mark.parametrize("input_mode", ["text", "transcript"])
@pytest.mark.parametrize("frustration", FRUSTRATIONS)
def test_customer_text_messages_accept_every_frustration_label(
    input_mode, frustration
):
    message = Message.model_validate(
        message_payload(input_mode=input_mode, frustration=frustration)
    )

    assert message.frustration == frustration


def test_legacy_customer_message_may_keep_null_labels():
    message = Message.model_validate(
        message_payload(sentiment=None, frustration=None)
    )

    assert message.sentiment is None
    assert message.frustration is None


@pytest.mark.parametrize("field,value", [("sentiment", "furious"), ("frustration", "low")])
def test_message_rejects_invalid_stored_labels(field, value):
    with pytest.raises(ValidationError):
        Message.model_validate({**message_payload(), field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "assistant", "input_mode": "text"},
        {"role": "assistant", "input_mode": "system"},
        {"role": "user", "input_mode": "system"},
    ],
    ids=["assistant-text", "assistant-system", "confirmation-system"],
)
@pytest.mark.parametrize(
    "labels", [{"sentiment": "neutral"}, {"frustration": "none"}], ids=str
)
def test_assistant_and_system_messages_carry_null_labels(overrides, labels):
    payload = message_payload(**{**overrides, "sentiment": None, "frustration": None})

    with pytest.raises(ValidationError):
        Message.model_validate({**payload, **labels})


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "assistant", "input_mode": "text"},
        {"role": "assistant", "input_mode": "system"},
        {"role": "user", "input_mode": "system"},
    ],
    ids=["assistant-text", "assistant-system", "confirmation-system"],
)
def test_assistant_and_system_messages_accept_null_labels(overrides):
    message = Message.model_validate(
        message_payload(**{**overrides, "sentiment": None, "frustration": None})
    )

    assert message.sentiment is None
    assert message.frustration is None


@pytest.mark.parametrize("value", [True, False])
def test_session_frustration_active_accepts_real_booleans(value):
    session = Session.model_validate({**session_payload(), "frustration_active": value})

    assert session.frustration_active is value


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, 1.0], ids=str)
def test_session_frustration_active_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        Session.model_validate({**session_payload(), "frustration_active": value})


def test_session_requires_frustration_active():
    payload = session_payload()
    del payload["frustration_active"]

    with pytest.raises(ValidationError):
        Session.model_validate(payload)


@pytest.mark.parametrize("value", [True, False])
def test_extraction_context_frustration_active_accepts_real_booleans(value):
    context = ExtractionContext.model_validate(
        {**extraction_context_payload(), "frustration_active": value}
    )

    assert context.frustration_active is value


@pytest.mark.parametrize("value", [0, 1, "true", None], ids=str)
def test_extraction_context_frustration_active_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        ExtractionContext.model_validate(
            {**extraction_context_payload(), "frustration_active": value}
        )


def test_extraction_context_requires_frustration_active():
    payload = extraction_context_payload()
    del payload["frustration_active"]

    with pytest.raises(ValidationError):
        ExtractionContext.model_validate(payload)


@pytest.mark.parametrize("value", [True, False])
def test_extraction_context_intake_started_accepts_real_booleans(value):
    context = ExtractionContext.model_validate(
        {**extraction_context_payload(), "intake_started": value}
    )

    assert context.intake_started is value


@pytest.mark.parametrize("value", [0, 1, "true", None], ids=str)
def test_extraction_context_intake_started_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        ExtractionContext.model_validate(
            {**extraction_context_payload(), "intake_started": value}
        )


def test_extraction_context_requires_intake_started():
    payload = extraction_context_payload()
    del payload["intake_started"]

    with pytest.raises(ValidationError):
        ExtractionContext.model_validate(payload)


@pytest.mark.parametrize("model,payload", [(SessionView, session_view_payload()), (TurnResponse, turn_response_payload())])
def test_served_objects_require_frustration_active(model, payload):
    without_flag = dict(payload)
    del without_flag["frustration_active"]

    with pytest.raises(ValidationError):
        model.model_validate(without_flag)


@pytest.mark.parametrize(
    "model,payload",
    [(SessionView, session_view_payload()), (TurnResponse, turn_response_payload())],
    ids=["SessionView", "TurnResponse"],
)
@pytest.mark.parametrize("value", [0, 1, "true", None], ids=str)
def test_served_frustration_active_is_a_strict_boolean(model, payload, value):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "frustration_active": value})


@pytest.mark.parametrize(
    "model,payload",
    [(SessionView, session_view_payload()), (TurnResponse, turn_response_payload())],
    ids=["SessionView", "TurnResponse"],
)
def test_served_objects_serialize_frustration_active(model, payload):
    dumped = model.model_validate({**payload, "frustration_active": True}).model_dump(
        mode="json"
    )

    assert dumped["frustration_active"] is True


def test_session_view_carries_stored_message_labels_without_inventing_them():
    view = SessionView.model_validate(
        {
            **session_view_payload(),
            "messages": [
                {
                    **message_payload(sentiment=None, frustration=None),
                    "speech_available": False,
                }
            ],
        }
    )

    assert view.messages[0].sentiment is None
    assert view.messages[0].frustration is None


def test_labels_stay_separate_from_business_fields():
    extraction = Extraction.model_validate(
        {
            **extraction_payload(),
            "sentiment": "negative",
            "frustration": "present",
            "candidates": {**slots_payload(), "urgency": None},
        }
    )

    assert extraction.candidates.urgency is None
    assert extraction.sentiment == "negative"
    assert extraction.frustration == "present"


def test_schema_version_two_is_required():
    session = Session.model_validate(session_payload())

    assert session.schema_version == 2
    assert str(session.session_id) == SESSION_ID
    assert session.session_id == UUID(SESSION_ID)


@pytest.mark.parametrize("value", [1, 3, 0, True, "2"], ids=str)
def test_other_schema_versions_are_rejected(value):
    with pytest.raises(ValidationError):
        Session.model_validate({**session_payload(), "schema_version": value})
