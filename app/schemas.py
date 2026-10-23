"""Strict Pydantic wire/session models (implementation contract section 4, RAG section 4).

Every object forbids extra keys. External request text uses StrictStr and
revisions use StrictInt; UUIDs are canonical lowercase on the wire; timestamps
are UTC with a trailing ``Z``. Models carry no network or filesystem effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

FieldName = Literal["order_number", "category", "description", "urgency"]
Status = Literal["collecting", "awaiting_confirmation", "completed"]
Intent = Literal[
    "provide_details",
    "correction",
    "confirm",
    "reject",
    "question",
    "unrelated",
    "social",
    "start_intake",
    "capability",
]
ClarificationCode = Literal["invalid", "ambiguous", "conflict"]
RequestInputMode = Literal["text", "transcript"]
MessageInputMode = Literal["text", "transcript", "system"]
MessageRole = Literal["user", "assistant"]

MAX_REQUEST_TEXT = 4000
SESSION_SCHEMA_VERSION = 1


def _canonical_uuid(value: object) -> UUID:
    """Accept a UUID instance or a canonical lowercase UUID string only."""

    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("must be a UUID or a canonical lowercase UUID string")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("must be a UUID or a canonical lowercase UUID string") from None
    if str(parsed) != value:
        raise ValueError("UUID string must be canonical lowercase")
    return parsed


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


CanonicalUUID = Annotated[UUID, BeforeValidator(_canonical_uuid)]
UtcTimestamp = Annotated[
    datetime,
    AfterValidator(_to_utc),
    PlainSerializer(_format_utc, return_type=str, when_used="json"),
]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
NonBlankText = Annotated[StrictStr, Field(min_length=1)]


class ContractModel(BaseModel):
    """Base model: unknown keys are always rejected."""

    model_config = ConfigDict(extra="forbid")


class Slots(ContractModel):
    order_number: StrictStr | None
    category: StrictStr | None
    description: StrictStr | None
    urgency: StrictStr | None


class Clarification(ContractModel):
    code: ClarificationCode
    candidate: StrictStr | None
    question: StrictStr


class RecentMessage(ContractModel):
    role: MessageRole
    text: StrictStr


class Extraction(ContractModel):
    intent: Intent
    has_question: StrictBool
    candidates: Slots
    explicit_corrections: list[FieldName]
    ambiguous_fields: list[FieldName]

    @field_validator("explicit_corrections", "ambiguous_fields")
    @classmethod
    def _unique_fields(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("field names must be unique")
        return value


class ExtractionContext(ContractModel):
    slots: Slots
    pending_clarifications: dict[FieldName, Clarification]
    recent_messages: list[RecentMessage]
    current_message: StrictStr
    status: Status


class TurnRequest(ContractModel):
    request_id: CanonicalUUID
    expected_revision: NonNegativeInt
    text: Annotated[StrictStr, Field(min_length=1, max_length=MAX_REQUEST_TEXT)]
    input_mode: RequestInputMode

    @field_validator("text", mode="before")
    @classmethod
    def _trim_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ConfirmRequest(ContractModel):
    request_id: CanonicalUUID
    expected_revision: NonNegativeInt
    review_revision: NonNegativeInt


class Citation(ContractModel):
    source_id: StrictStr
    title: StrictStr
    excerpt: StrictStr
    kb_version: StrictStr


class Message(ContractModel):
    id: CanonicalUUID
    role: MessageRole
    text: NonBlankText
    input_mode: MessageInputMode
    created_at: UtcTimestamp
    citations: list[Citation] = Field(default_factory=list)


class SessionView(ContractModel):
    session_id: CanonicalUUID
    revision: NonNegativeInt
    status: Status
    slots: Slots
    messages: list[Message]
    pending_clarifications: dict[FieldName, Clarification]
    pending_review_revision: NonNegativeInt | None
    summary: StrictStr | None


class TurnResponse(ContractModel):
    session_id: CanonicalUUID
    revision: NonNegativeInt
    status: Status
    reply: NonBlankText
    message_id: CanonicalUUID
    slots: Slots
    pending_clarifications: dict[FieldName, Clarification]
    pending_review_revision: NonNegativeInt | None
    summary: StrictStr | None
    citations: list[Citation] = Field(default_factory=list)


class Confirmation(ContractModel):
    confirmed_at: UtcTimestamp
    review_revision: NonNegativeInt


class ReplayRecord(ContractModel):
    request_id: CanonicalUUID
    input_digest: StrictStr
    response: TurnResponse


class Session(ContractModel):
    schema_version: StrictInt
    session_id: CanonicalUUID
    revision: NonNegativeInt
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    status: Status
    slots: Slots
    messages: list[Message]
    pending_clarifications: dict[FieldName, Clarification]
    pending_review_revision: NonNegativeInt | None
    review_rejected: StrictBool
    collection_turn_count: NonNegativeInt
    summary: StrictStr | None
    confirmation: Confirmation | None
    requests: list[ReplayRecord]

    @field_validator("schema_version")
    @classmethod
    def _schema_version_one(cls, value: int) -> int:
        if value != SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported session schema_version")
        return value


class KnowledgeRecord(ContractModel):
    id: StrictStr
    title: StrictStr
    keywords: list[StrictStr]
    body: StrictStr


class KnowledgeContext(ContractModel):
    question: StrictStr
    kb_version: StrictStr
    sources: list[KnowledgeRecord]


class KnowledgeAnswer(ContractModel):
    answerable: StrictBool
    answer: StrictStr
    cited_ids: list[StrictStr]
