"""Test doubles and builders for the storage/engine unit slices.

Only real external boundaries are doubled here: the AI provider (a remote
service) and the knowledge base lookup . Every
call is scripted; an unexpected call raises so a turn cannot silently use a
different provider operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID, uuid4

from backend.gateway.providers.client import SynthesizedAudio
from backend.gateway.schemas import (
    Extraction,
    KnowledgeAnswer,
    KnowledgeRecord,
    Message,
    Session,
    Slots,
)

NO_SLOTS: dict[str, str | None] = {
    "order_number": None,
    "category": None,
    "description": None,
    "urgency": None,
}


def make_slots(
    order_number: str | None = None,
    category: str | None = None,
    description: str | None = None,
    urgency: str | None = None,
) -> Slots:
    return Slots(
        order_number=order_number,
        category=category,
        description=description,
        urgency=urgency,
    )


def make_extraction(
    *,
    intent: str = "provide_details",
    has_question: bool = False,
    candidates: dict[str, str | None] | None = None,
    explicit_corrections: Iterable[str] = (),
    ambiguous_fields: Iterable[str] = (),
    detected_language: str = "en",
    requested_language: str | None = None,
    sentiment: str = "neutral",
    frustration: str = "none",
) -> Extraction:
    return Extraction(
        intent=intent,
        has_question=has_question,
        candidates=Slots(**{**NO_SLOTS, **(candidates or {})}),
        explicit_corrections=list(explicit_corrections),
        ambiguous_fields=list(ambiguous_fields),
        detected_language=detected_language,
        requested_language=requested_language,
        sentiment=sentiment,
        frustration=frustration,
    )


def make_record(
    record_id: str = "faq-returns",
    title: str = "Returns and refunds",
    keywords: Iterable[str] = ("return", "returns", "refund", "refunds"),
    body: str = "An unused item can be requested for return within 30 calendar days.",
) -> KnowledgeRecord:
    return KnowledgeRecord(
        id=record_id, title=title, keywords=list(keywords), body=body
    )


def make_session(
    *,
    session_id: UUID | None = None,
    revision: int = 0,
    status: str = "collecting",
    reply_language: str = "en",
    frustration_active: bool = False,
    slots=None,
    messages: list | None = None,
    pending_clarifications=None,
    pending_review_revision=None,
    review_rejected: bool = False,
    collection_turn_count: int = 0,
    summary: str | None = None,
    confirmation=None,
    requests: list | None = None,
) -> Session:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return Session(
        schema_version=2,
        session_id=session_id or uuid4(),
        revision=revision,
        created_at=now,
        updated_at=now,
        status=status,
        reply_language=reply_language,
        frustration_active=frustration_active,
        slots=slots or make_slots(),
        messages=messages or [],
        pending_clarifications=pending_clarifications or {},
        pending_review_revision=pending_review_revision,
        review_rejected=review_rejected,
        collection_turn_count=collection_turn_count,
        summary=summary,
        confirmation=confirmation,
        requests=requests or [],
    )


def make_message(text: str = "Hello", role: str = "assistant") -> Message:
    return Message(
        id=uuid4(),
        role=role,
        text=text,
        input_mode="system" if role == "assistant" else "text",
        created_at=datetime.now(timezone.utc).replace(microsecond=0),
        sentiment=None if role == "assistant" else "neutral",
        frustration=None if role == "assistant" else "none",
    )


class FakeProvider:
    """In-memory AIProvider that replays a scripted list of results."""

    def __init__(self, script: Iterable[tuple[str, object]] = ()) -> None:
        self._script: list[tuple[str, object]] = list(script)
        self.calls: list[tuple[str, object]] = []

    def _next(self, method: str, context: object) -> object:
        if not self._script:
            raise AssertionError(f"unexpected provider call: {method}")
        expected, result = self._script.pop(0)
        if expected != method:
            raise AssertionError(
                f"unexpected provider call: {method} (scripted {expected})"
            )
        self.calls.append((method, context))
        return result

    async def extract(self, context: object) -> Extraction:
        return self._next("extract", context)  # type: ignore[return-value]

    async def answer_knowledge(self, context: object) -> KnowledgeAnswer:
        return self._next("answer_knowledge", context)  # type: ignore[return-value]

    async def transcribe(self, audio: bytes) -> str:
        raise AssertionError("unexpected provider call: transcribe")

    async def synthesize(self, text: str) -> SynthesizedAudio:
        raise AssertionError("unexpected provider call: synthesize")

    async def close(self) -> None:
        return None

    def assert_exhausted(self) -> None:
        assert self._script == [], f"unused scripted results: {self._script}"


class SlowProvider(FakeProvider):
    """FakeProvider whose scripted calls sleep to exercise engine deadlines."""

    def __init__(self, script=(), *, delay: float) -> None:
        super().__init__(script)
        self.delay = delay

    async def extract(self, context: object):
        import asyncio

        await asyncio.sleep(self.delay)
        return self._next("extract", context)

    async def answer_knowledge(self, context: object):
        import asyncio

        await asyncio.sleep(self.delay)
        return self._next("answer_knowledge", context)


class FakeKnowledgeBase:
    """Scripted immutable knowledge base: exact question -> retrieved records."""

    def __init__(
        self,
        answers: dict[str, list[KnowledgeRecord]] | None = None,
        *,
        version: str = "2026-10-22",
    ) -> None:
        self.version = version
        self._answers = dict(answers or {})
        self.questions: list[str] = []

    def retrieve(self, question: str) -> list[KnowledgeRecord]:
        self.questions.append(question)
        if question not in self._answers:
            raise AssertionError(f"unexpected retrieval: {question!r}")
        return list(self._answers[question])
