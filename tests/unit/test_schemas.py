"""Unit tests for the strict wire/session models (IC section 4, RAG section 4).

The payload factories below are the contract shapes transcribed literally from
the implementation contract; strictness rules are asserted in the second slice
of this file.
"""

import pytest
from pydantic import ValidationError

from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.schemas import (
    Clarification,
    ConfirmRequest,
    Confirmation,
    Extraction,
    ExtractionContext,
    FieldName,
    Intent,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
    Message,
    RecentMessage,
    ReplayRecord,
    Session,
    SessionView,
    Slots,
    Status,
    TurnRequest,
    TurnResponse,
    Citation,
)

SESSION_ID = "3f7c1c1e-0f0a-4a4e-9a5e-2b1d0f6c8a11"
MESSAGE_ID = "b1e0f7a2-5c4d-4e6f-8a9b-0c1d2e3f4a5b"
REQUEST_ID = "a9d8c7b6-5e4f-4a3b-9c2d-1e0f2a3b4c5d"
CREATED_AT = "2026-10-22T12:00:00Z"


def slots_payload():
    return {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }


def clarification_payload():
    return {"code": "invalid", "candidate": None, "question": "Please clarify."}


def citation_payload():
    return {
        "source_id": "faq-delivery",
        "title": "Delivery times",
        "excerpt": "Standard delivery takes 2 to 5 business days after dispatch.",
        "kb_version": "2026-10-22",
    }


def message_payload():
    return {
        "id": MESSAGE_ID,
        "role": "assistant",
        "text": "Hello.",
        "input_mode": "text",
        "created_at": CREATED_AT,
        "citations": [],
    }


def session_view_payload():
    return {
        "session_id": SESSION_ID,
        "revision": 0,
        "status": "collecting",
        "slots": slots_payload(),
        "messages": [],
        "pending_clarifications": {},
        "pending_review_revision": None,
        "summary": None,
    }


def turn_response_payload():
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
    }


def replay_record_payload():
    return {
        "request_id": REQUEST_ID,
        "input_digest": "0" * 64,
        "response": turn_response_payload(),
    }


def knowledge_record_payload():
    return {
        "id": "faq-delivery",
        "title": "Delivery times",
        "keywords": ["delivery", "shipping", "ship", "arrive", "arrives"],
        "body": "Standard delivery takes 2 to 5 business days after dispatch.",
    }


def model_payloads():
    return {
        Slots: slots_payload(),
        Clarification: clarification_payload(),
        RecentMessage: {"role": "user", "text": "My order arrived broken."},
        Extraction: {
            "intent": "provide_details",
            "has_question": False,
            "candidates": slots_payload(),
            "explicit_corrections": [],
            "ambiguous_fields": [],
        },
        ExtractionContext: {
            "slots": slots_payload(),
            "pending_clarifications": {},
            "recent_messages": [],
            "current_message": "My order arrived broken.",
            "status": "collecting",
        },
        TurnRequest: {
            "request_id": REQUEST_ID,
            "expected_revision": 0,
            "text": "My order arrived broken.",
            "input_mode": "text",
        },
        ConfirmRequest: {
            "request_id": REQUEST_ID,
            "expected_revision": 1,
            "review_revision": 1,
        },
        Citation: citation_payload(),
        Message: message_payload(),
        SessionView: session_view_payload(),
        TurnResponse: turn_response_payload(),
        Confirmation: {"confirmed_at": CREATED_AT, "review_revision": 1},
        ReplayRecord: replay_record_payload(),
        Session: {
            "schema_version": 1,
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
        },
        KnowledgeRecord: knowledge_record_payload(),
        KnowledgeContext: {
            "question": "How long does delivery take?",
            "kb_version": "2026-10-22",
            "sources": [knowledge_record_payload()],
        },
        KnowledgeAnswer: {
            "answerable": True,
            "answer": "Standard delivery takes 2 to 5 business days.",
            "cited_ids": ["faq-delivery"],
        },
    }


MODELS = model_payloads()
MODEL_CASES = [(model, payload) for model, payload in MODELS.items()]
MODEL_IDS = [model.__name__ for model in MODELS]


@pytest.mark.parametrize("model,payload", MODEL_CASES, ids=MODEL_IDS)
def test_models_forbid_extra_keys(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unknown_key": "value"})


@pytest.mark.parametrize("model,payload", MODEL_CASES, ids=MODEL_IDS)
def test_models_accept_the_contract_shape_and_round_trip(model, payload):
    instance = model.model_validate(payload)

    assert isinstance(instance, model)
    assert model.model_validate(instance.model_dump(mode="json")) == instance


@pytest.mark.parametrize(
    "missing_key",
    ["order_number", "category", "description", "urgency"],
)
def test_slots_require_all_four_keys(missing_key):
    payload = slots_payload()
    del payload[missing_key]

    with pytest.raises(ValidationError):
        Slots.model_validate(payload)


@pytest.mark.parametrize(
    "missing_key",
    ["intent", "has_question", "candidates", "explicit_corrections", "ambiguous_fields"],
)
def test_extraction_requires_every_contract_key(missing_key):
    payload = MODELS[Extraction].copy()
    del payload[missing_key]

    with pytest.raises(ValidationError):
        Extraction.model_validate(payload)


@pytest.mark.parametrize(
    "missing_key",
    [
        "schema_version",
        "session_id",
        "revision",
        "created_at",
        "updated_at",
        "status",
        "slots",
        "messages",
        "pending_clarifications",
        "pending_review_revision",
        "review_rejected",
        "collection_turn_count",
        "summary",
        "confirmation",
        "requests",
    ],
)
def test_session_requires_every_stored_key(missing_key):
    payload = dict(MODELS[Session])
    del payload[missing_key]

    with pytest.raises(ValidationError):
        Session.model_validate(payload)


@pytest.mark.parametrize("value", ["order_number", "category", "description", "urgency"])
def test_field_name_literal_accepts_contract_values(value):
    extraction = Extraction.model_validate(
        {**MODELS[Extraction], "explicit_corrections": [value]}
    )

    assert extraction.explicit_corrections == [value]


def test_field_name_literal_rejects_unknown_values():
    with pytest.raises(ValidationError):
        Extraction.model_validate({**MODELS[Extraction], "explicit_corrections": ["id"]})


@pytest.mark.parametrize(
    "value", ["collecting", "awaiting_confirmation", "completed"]
)
def test_status_literal_accepts_contract_values(value):
    view = SessionView.model_validate({**MODELS[SessionView], "status": value})

    assert view.status == value


def test_status_literal_rejects_unknown_values():
    with pytest.raises(ValidationError):
        SessionView.model_validate({**MODELS[SessionView], "status": "done"})


@pytest.mark.parametrize(
    "value",
    [
        "provide_details",
        "correction",
        "confirm",
        "reject",
        "question",
        "unrelated",
        "social",
        "start_intake",
        "capability",
    ],
)
def test_intent_literal_accepts_contract_values(value):
    extraction = Extraction.model_validate({**MODELS[Extraction], "intent": value})

    assert extraction.intent == value


def test_intent_literal_rejects_unknown_values():
    with pytest.raises(ValidationError):
        Extraction.model_validate({**MODELS[Extraction], "intent": "greeting"})


@pytest.mark.parametrize("value", ["invalid", "ambiguous", "conflict"])
def test_clarification_code_literal_accepts_contract_values(value):
    clarification = Clarification.model_validate(
        {**clarification_payload(), "code": value}
    )

    assert clarification.code == value


def test_clarification_code_literal_rejects_unknown_values():
    with pytest.raises(ValidationError):
        Clarification.model_validate({**clarification_payload(), "code": "other"})


@pytest.mark.parametrize("value", ["user", "assistant"])
def test_message_role_literal_accepts_contract_values(value):
    message = Message.model_validate({**message_payload(), "role": value})

    assert message.role == value


def test_message_role_literal_rejects_system():
    with pytest.raises(ValidationError):
        Message.model_validate({**message_payload(), "role": "system"})


@pytest.mark.parametrize("value", ["text", "transcript", "system"])
def test_message_input_mode_literal_accepts_contract_values(value):
    message = Message.model_validate({**message_payload(), "input_mode": value})

    assert message.input_mode == value


@pytest.mark.parametrize("value", ["text", "transcript"])
def test_turn_request_input_mode_literal_accepts_contract_values(value):
    request = TurnRequest.model_validate({**MODELS[TurnRequest], "input_mode": value})

    assert request.input_mode == value


def test_turn_request_input_mode_literal_rejects_system():
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**MODELS[TurnRequest], "input_mode": "system"})


def test_message_citations_default_to_empty_list():
    payload = message_payload()
    del payload["citations"]

    assert Message.model_validate(payload).citations == []


def test_turn_response_citations_default_to_empty_list():
    payload = turn_response_payload()
    del payload["citations"]

    assert TurnResponse.model_validate(payload).citations == []


def test_turn_response_carries_the_assistant_message_id_key():
    response = TurnResponse.model_validate(MODELS[TurnResponse])

    assert str(response.message_id) == MESSAGE_ID
    assert "assistant_message_id" not in response.model_dump()


def test_pending_clarifications_keys_are_field_names():
    payload = {
        **MODELS[SessionView],
        "pending_clarifications": {"urgency": clarification_payload()},
    }
    view = SessionView.model_validate(payload)

    assert set(view.pending_clarifications) == {"urgency"}


def test_pending_clarifications_reject_unknown_field_keys():
    payload = {
        **MODELS[SessionView],
        "pending_clarifications": {"unknown_field": clarification_payload()},
    }

    with pytest.raises(ValidationError):
        SessionView.model_validate(payload)


def test_field_name_and_status_aliases_are_the_contract_literals():
    assert FieldName is not None
    assert Status is not None
    assert Intent is not None


# --- Strictness slice: UUID, StrictInt, StrictStr, text limit, UTC, version ---

UUID_FIELDS = [
    (TurnRequest, MODELS[TurnRequest], "request_id"),
    (ConfirmRequest, MODELS[ConfirmRequest], "request_id"),
    (Message, message_payload(), "id"),
    (SessionView, session_view_payload(), "session_id"),
    (TurnResponse, turn_response_payload(), "message_id"),
    (Session, dict(MODELS[Session]), "session_id"),
    (ReplayRecord, replay_record_payload(), "request_id"),
]
UUID_FIELD_IDS = [f"{model.__name__}.{field}" for model, _, field in UUID_FIELDS]

INT_FIELDS = [
    (TurnRequest, MODELS[TurnRequest], "expected_revision"),
    (ConfirmRequest, MODELS[ConfirmRequest], "expected_revision"),
    (ConfirmRequest, MODELS[ConfirmRequest], "review_revision"),
    (SessionView, session_view_payload(), "revision"),
    (SessionView, session_view_payload(), "pending_review_revision"),
    (TurnResponse, turn_response_payload(), "revision"),
    (Session, dict(MODELS[Session]), "revision"),
    (Session, dict(MODELS[Session]), "collection_turn_count"),
    (Confirmation, {"confirmed_at": CREATED_AT, "review_revision": 1}, "review_revision"),
]
INT_FIELD_IDS = [f"{model.__name__}.{field}" for model, _, field in INT_FIELDS]


@pytest.mark.parametrize("model,payload,field", UUID_FIELDS, ids=UUID_FIELD_IDS)
def test_uuid_fields_accept_a_uuid_instance(model, payload, field):
    payload = dict(payload)
    payload[field] = UUID(SESSION_ID)

    assert getattr(model.model_validate(payload), field) == UUID(SESSION_ID)


@pytest.mark.parametrize("model,payload,field", UUID_FIELDS, ids=UUID_FIELD_IDS)
def test_uuid_fields_accept_a_canonical_lowercase_string(model, payload, field):
    payload = dict(payload)
    payload[field] = SESSION_ID
    instance = model.model_validate(payload)

    assert getattr(instance, field) == UUID(SESSION_ID)
    assert instance.model_dump(mode="json")[field] == SESSION_ID


@pytest.mark.parametrize(
    "value",
    [
        SESSION_ID.upper(),
        "{" + SESSION_ID + "}",
        "urn:uuid:" + SESSION_ID,
        SESSION_ID + " ",
        "not-a-uuid",
        123,
    ],
    ids=["uppercase", "braces", "urn", "trailing-space", "garbage", "int"],
)
@pytest.mark.parametrize("model,payload,field", UUID_FIELDS, ids=UUID_FIELD_IDS)
def test_uuid_fields_reject_non_canonical_input(model, payload, field, value):
    payload = dict(payload)
    payload[field] = value

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("model,payload,field", INT_FIELDS, ids=INT_FIELD_IDS)
@pytest.mark.parametrize("value", [True, False, -1, "3", 3.0], ids=str)
def test_integer_fields_are_strict_and_nonnegative(model, payload, field, value):
    payload = dict(payload)
    payload[field] = value

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("model,payload,field", INT_FIELDS, ids=INT_FIELD_IDS)
@pytest.mark.parametrize("value", [0, 7])
def test_integer_fields_accept_nonnegative_integers(model, payload, field, value):
    payload = dict(payload)
    payload[field] = value

    assert getattr(model.model_validate(payload), field) == value


@pytest.mark.parametrize("value", [123, True, None, ["text"]], ids=str)
def test_turn_request_text_is_a_strict_string(value):
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**MODELS[TurnRequest], "text": value})


@pytest.mark.parametrize("value", [123, True, None], ids=str)
def test_message_text_is_a_strict_string(value):
    with pytest.raises(ValidationError):
        Message.model_validate({**message_payload(), "text": value})


def test_turn_request_text_is_trimmed():
    request = TurnRequest.model_validate(
        {**MODELS[TurnRequest], "text": "  My order arrived broken.  "}
    )

    assert request.text == "My order arrived broken."


@pytest.mark.parametrize("value", ["", "   ", "\t\n "])
def test_turn_request_text_must_be_nonblank(value):
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**MODELS[TurnRequest], "text": value})


def test_turn_request_text_accepts_exactly_4000_characters():
    request = TurnRequest.model_validate(
        {**MODELS[TurnRequest], "text": "a" * 4000}
    )

    assert len(request.text) == 4000


def test_turn_request_text_rejects_4001_characters():
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**MODELS[TurnRequest], "text": "a" * 4001})


def test_turn_request_text_length_is_measured_after_trimming():
    request = TurnRequest.model_validate(
        {**MODELS[TurnRequest], "text": "  " + "a" * 4000 + "  "}
    )

    assert len(request.text) == 4000


@pytest.mark.parametrize("value", [1, 0, "true", "false", None], ids=str)
def test_has_question_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        Extraction.model_validate({**MODELS[Extraction], "has_question": value})


@pytest.mark.parametrize("value", [True, False])
def test_has_question_accepts_real_booleans(value):
    extraction = Extraction.model_validate({**MODELS[Extraction], "has_question": value})

    assert extraction.has_question is value


def test_explicit_corrections_must_be_unique():
    with pytest.raises(ValidationError):
        Extraction.model_validate(
            {**MODELS[Extraction], "explicit_corrections": ["urgency", "urgency"]}
        )


def test_ambiguous_fields_must_be_unique():
    with pytest.raises(ValidationError):
        Extraction.model_validate(
            {**MODELS[Extraction], "ambiguous_fields": ["category", "category"]}
        )


@pytest.mark.parametrize("value", [2, 0, True, "1"], ids=str)
def test_session_schema_version_must_be_exactly_one(value):
    with pytest.raises(ValidationError):
        Session.model_validate({**MODELS[Session], "schema_version": value})


def test_session_schema_version_one_is_accepted():
    assert Session.model_validate(MODELS[Session]).schema_version == 1


def test_message_created_at_serializes_with_trailing_z():
    payload = {
        **message_payload(),
        "created_at": datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    }

    assert Message.model_validate(payload).model_dump(mode="json")["created_at"] == (
        "2026-10-22T12:00:00Z"
    )


def test_session_timestamps_serialize_with_trailing_z():
    payload = {
        **MODELS[Session],
        "created_at": datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc),
        "confirmation": {
            "confirmed_at": datetime(2026, 10, 22, 12, 45, tzinfo=timezone.utc),
            "review_revision": 3,
        },
    }
    dumped = Session.model_validate(payload).model_dump(mode="json")

    assert dumped["created_at"] == "2026-10-22T12:00:00Z"
    assert dumped["updated_at"] == "2026-10-22T12:30:00Z"
    assert dumped["confirmation"]["confirmed_at"] == "2026-10-22T12:45:00Z"


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        Message.model_validate(
            {**message_payload(), "created_at": datetime(2026, 10, 22, 12, 0)}
        )


def test_offset_timestamps_are_normalized_to_utc_z():
    payload = {
        **message_payload(),
        "created_at": datetime(
            2026, 10, 22, 14, 0, tzinfo=timezone(timedelta(hours=2))
        ),
    }

    assert Message.model_validate(payload).model_dump(mode="json")["created_at"] == (
        "2026-10-22T12:00:00Z"
    )


@pytest.mark.parametrize("value", [123, True, ["NS-123456"]], ids=str)
def test_slot_values_are_strings_or_null(value):
    with pytest.raises(ValidationError):
        Slots.model_validate({**slots_payload(), "order_number": value})


def test_candidates_tolerate_invalid_business_values_as_strings():
    extraction = Extraction.model_validate(
        {
            **MODELS[Extraction],
            "candidates": {
                "order_number": "12345",
                "category": "banana",
                "description": "x",
                "urgency": "extremely",
            },
        }
    )

    assert extraction.candidates.category == "banana"
    assert extraction.candidates.urgency == "extremely"

