"""Conversation engine: durable turn transaction, merge and reply selection.

One repository lock spans load/dedup/check/extract/merge/save. The engine holds
no global singletons: every instance owns its repository, provider and
knowledge base, so tests isolate cleanly (IC §3-§8, RAG §5-§6).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from app.errors import AppError, app_error
from app.localization import (
    build_review_reply,
    build_summary,
    missing_question,
    text,
)
from app.schemas import (
    Citation,
    Clarification,
    Confirmation,
    ConfirmRequest,
    Extraction,
    ExtractionContext,
    FieldName,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
    Language,
    Message,
    MessageView,
    ReplayRecord,
    Session,
    SessionView,
    Slots,
    TurnRequest,
    TurnResponse,
)
from app.storage import JsonSessionRepository
from app.validation import (
    FIELD_ORDER,
    MergeOutcome,
    merge_extraction,
    normalize_candidate,
    validate_field,
)

RECENT_MESSAGE_LIMIT = 12
MAX_COLLECTION_TURNS = 50
TEXT_TURN_DEADLINE_SECONDS = 25.0
MAX_KNOWLEDGE_ANSWER_CHARS = 1200
TURN_ENDPOINT = "turn"
CONFIRM_ENDPOINT = "confirm"
CONFIRM_USER_TEXT = "Confirm reviewed intake"


class AIProvider(Protocol):
    async def extract(self, context: ExtractionContext): ...

    async def answer_knowledge(self, context): ...

    async def transcribe(self, audio: bytes) -> str: ...

    async def synthesize(self, text: str) -> bytes: ...

    async def close(self) -> None: ...


class KnowledgeBase(Protocol):
    version: str

    def retrieve(self, question: str) -> list[KnowledgeRecord]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _digest(endpoint: str, session_id: UUID, body: dict) -> str:
    canonical = json.dumps(
        {"endpoint": endpoint, "session_id": str(session_id), **body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def turn_digest(session_id: UUID, command: TurnRequest) -> str:
    return _digest(
        TURN_ENDPOINT,
        session_id,
        {
            "request_id": str(command.request_id),
            "expected_revision": command.expected_revision,
            "text": command.text,
            "input_mode": command.input_mode,
        },
    )


def confirm_digest(session_id: UUID, command: ConfirmRequest) -> str:
    return _digest(
        CONFIRM_ENDPOINT,
        session_id,
        {
            "request_id": str(command.request_id),
            "expected_revision": command.expected_revision,
            "review_revision": command.review_revision,
        },
    )


def _find_replay(session: Session, request_id: UUID) -> ReplayRecord | None:
    for record in session.requests:
        if record.request_id == request_id:
            return record
    return None


def _view(session: Session, speech_languages: frozenset[str]) -> SessionView:
    return SessionView(
        session_id=session.session_id,
        revision=session.revision,
        status=session.status,
        reply_language=session.reply_language,
        slots=session.slots,
        messages=[
            MessageView(
                **message.model_dump(),
                speech_available=_speech_available(message, speech_languages),
            )
            for message in session.messages
        ],
        pending_clarifications=session.pending_clarifications,
        pending_review_revision=session.pending_review_revision,
        summary=session.summary,
    )


def _speech_available(message: Message, speech_languages: frozenset[str]) -> bool:
    """Playback availability from the saved message language and operator profiles."""

    return message.role == "assistant" and message.language in speech_languages


def _revision_conflict(current_revision: int) -> AppError:
    error = app_error("revision_conflict")
    error.current_revision = current_revision
    return error


def _extraction_context(session: Session, current_message: str) -> ExtractionContext:
    recent = [
        {"role": message.role, "text": message.text}
        for message in session.messages[-RECENT_MESSAGE_LIMIT:]
    ]
    return ExtractionContext(
        slots=session.slots,
        pending_clarifications=session.pending_clarifications,
        recent_messages=recent,
        current_message=current_message,
        status=session.status,
    )


def _first_issue(pending: dict[FieldName, Clarification]) -> Clarification | None:
    for field in FIELD_ORDER:
        clarification = pending.get(field)
        if clarification is not None:
            return clarification
    return None


def _first_missing(slots: Slots, language: Language) -> str | None:
    for field in FIELD_ORDER:
        if getattr(slots, field) is None:
            return missing_question(field, language)
    return None


def _reply_language(session: Session, extraction: Extraction) -> Language:
    """An explicit requested language wins; otherwise a supported detection does."""

    if extraction.requested_language is not None:
        return extraction.requested_language
    if extraction.detected_language in ("en", "es"):
        return extraction.detected_language
    return session.reply_language


def _unchanged_outcome(session: Session) -> MergeOutcome:
    """Outcome that accepts nothing, for input the demo cannot understand."""

    return MergeOutcome(
        slots=session.slots,
        pending_clarifications=session.pending_clarifications,
        accepted_fields=frozenset(),
        cleared_fields=frozenset(),
    )


def _slots_complete(slots: Slots) -> bool:
    return all(getattr(slots, field) is not None for field in FIELD_ORDER)


class ConversationEngine:
    """Owns one session's turn transaction from load to atomic save."""

    def __init__(
        self,
        repository: JsonSessionRepository,
        provider: AIProvider,
        knowledge_base: KnowledgeBase,
        *,
        speech_languages: frozenset[str] = frozenset({"en"}),
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.knowledge_base = knowledge_base
        self.speech_languages = speech_languages

    async def create_session(self) -> SessionView:
        now = _now()
        session_id = uuid4()
        greeting = Message(
            id=uuid4(),
            role="assistant",
            text=text("greeting", "en"),
            input_mode="system",
            created_at=now,
            citations=[],
            language="en",
        )
        session = Session(
            schema_version=1,
            session_id=session_id,
            revision=0,
            created_at=now,
            updated_at=now,
            status="collecting",
            reply_language="en",
            slots=Slots(
                order_number=None,
                category=None,
                description=None,
                urgency=None,
            ),
            messages=[greeting],
            pending_clarifications={},
            pending_review_revision=None,
            review_rejected=False,
            collection_turn_count=0,
            summary=None,
            confirmation=None,
            requests=[],
        )
        async with self.repository.locked(session_id):
            await self.repository.save(session)
        return _view(session, self.speech_languages)

    async def get_session(self, session_id: UUID) -> SessionView:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
        return _view(session, self.speech_languages)

    async def turn(self, session_id: UUID, command: TurnRequest) -> TurnResponse:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            digest = turn_digest(session_id, command)
            replay = _find_replay(session, command.request_id)
            if replay is not None:
                if replay.input_digest != digest:
                    raise app_error("request_id_conflict")
                return replay.response
            if command.expected_revision != session.revision:
                raise _revision_conflict(session.revision)
            if session.status == "completed":
                raise app_error("session_completed")

            at_limit = session.collection_turn_count >= MAX_COLLECTION_TURNS
            if at_limit and not _current_review_valid(session):
                raise app_error("turn_limit")

            try:
                async with asyncio.timeout(TEXT_TURN_DEADLINE_SECONDS):
                    extraction = await self.provider.extract(
                        _extraction_context(session, command.text)
                    )
                    reply_language = _reply_language(session, extraction)
                    if extraction.detected_language == "unsupported":
                        if at_limit:
                            raise app_error("turn_limit")
                        outcome = _unchanged_outcome(session)
                        plan = _unsupported_plan(session, extraction, reply_language)
                    else:
                        outcome = merge_extraction(
                            session.slots,
                            session.pending_clarifications,
                            extraction,
                            language=reply_language,
                        )
                        if at_limit:
                            review_rejected, recovered_this_turn = _apply_transitions(
                                session, extraction, outcome
                            )
                            if not _is_pure_confirmation(
                                session,
                                extraction,
                                outcome,
                                review_rejected=review_rejected,
                                recovered_this_turn=recovered_this_turn,
                                current_review_valid=_current_review_valid(session),
                            ):
                                raise app_error("turn_limit")
                        plan = await self._plan_turn(
                            session,
                            command,
                            extraction,
                            outcome,
                            reply_language,
                            session.revision + 1,
                        )
            except TimeoutError:
                raise app_error("provider_timeout") from None
            return await self._commit_turn(session, command, extraction, outcome, plan)

    async def assistant_message(self, session_id: UUID, message_id: UUID) -> Message:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            for message in session.messages:
                if message.id == message_id and message.role == "assistant":
                    return message
        raise app_error("message_not_found")

    async def assistant_text(self, session_id: UUID, message_id: UUID) -> str:
        return (await self.assistant_message(session_id, message_id)).text

    async def confirm(
        self, session_id: UUID, command: ConfirmRequest
    ) -> TurnResponse:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            digest = confirm_digest(session_id, command)
            replay = _find_replay(session, command.request_id)
            if replay is not None:
                if replay.input_digest != digest:
                    raise app_error("request_id_conflict")
                return replay.response
            if command.expected_revision != session.revision:
                raise _revision_conflict(session.revision)
            if session.status == "completed":
                raise app_error("session_completed")
            if session.pending_review_revision is None:
                # No review exists (incomplete, rejected or issue-blocked intake):
                # nothing to confirm and nothing that could be stale (IC §7, §13.2 IT04-18/19).
                raise app_error("review_not_ready")
            if command.review_revision != session.pending_review_revision:
                raise app_error("stale_review")
            if not _current_review_valid(session):
                raise app_error("review_not_ready")

            now = _now()
            new_revision = session.revision + 1
            reply = text("receipt", session.reply_language).format(
                session_id=session.session_id
            )
            user_message = Message(
                id=uuid4(),
                role="user",
                text=CONFIRM_USER_TEXT,
                input_mode="system",
                created_at=now,
                citations=[],
                language=session.reply_language,
            )
            assistant_message = Message(
                id=uuid4(),
                role="assistant",
                text=reply,
                input_mode="system",
                created_at=now,
                citations=[],
                language=session.reply_language,
            )
            response = TurnResponse(
                session_id=session.session_id,
                revision=new_revision,
                status="completed",
                reply_language=session.reply_language,
                reply=reply,
                message_id=assistant_message.id,
                slots=session.slots,
                pending_clarifications={},
                pending_review_revision=None,
                summary=session.summary,
                citations=[],
            )
            committed = session.model_copy(deep=True)
            committed.revision = new_revision
            committed.updated_at = now
            committed.status = "completed"
            committed.pending_clarifications = {}
            committed.pending_review_revision = None
            committed.confirmation = Confirmation(
                confirmed_at=now, review_revision=command.review_revision
            )
            committed.messages = [
                *session.messages,
                user_message,
                assistant_message,
            ]
            committed.requests = [
                *session.requests,
                ReplayRecord(
                    request_id=command.request_id,
                    input_digest=digest,
                    response=response,
                ),
            ]
            await self.repository.save(committed)
            return response

    async def _plan_turn(
        self,
        session: Session,
        command: TurnRequest,
        extraction: Extraction,
        outcome: MergeOutcome,
        reply_language: Language,
        new_revision: int,
    ) -> "TurnPlan":
        pending = outcome.pending_clarifications
        review_rejected, recovered_this_turn = _apply_transitions(
            session, extraction, outcome
        )
        current_review_valid = _current_review_valid(session)
        has_active_review = session.pending_review_revision is not None
        language_only = _is_language_only(extraction)
        pure_social = _is_pure_social(extraction) and not language_only
        pure_capability = _is_pure_capability(extraction) and not language_only
        pure_unrelated = _is_pure_unrelated(extraction) and not language_only
        pure_question = _is_pure_question(extraction)
        pure_confirmation = _is_pure_confirmation(
            session,
            extraction,
            outcome,
            review_rejected=review_rejected,
            recovered_this_turn=recovered_this_turn,
            current_review_valid=current_review_valid,
        ) and not language_only

        summary = session.summary
        review_revision = session.pending_review_revision
        status = session.status
        citations: tuple[Citation, ...] = ()
        completed = False
        intake_directed = False

        if extraction.intent == "reject":
            intake_directed = True
            if has_active_review or session.review_rejected:
                reply = text("reject_prompt", reply_language)
                status = "collecting"
                summary = None
                review_revision = None
            else:
                reply = text("no_review_to_reject", reply_language)
        elif pure_social:
            reply = text("social_acknowledgment", reply_language)
        elif pure_capability:
            reply = text("capability_reply", reply_language)
        elif pure_question:
            reply, citations = await self._answer_policy_question(
                command.text, reply_language
            )
        elif pure_confirmation:
            reply = text("receipt", reply_language).format(
                session_id=session.session_id
            )
            status = "completed"
            summary = session.summary
            review_revision = None
            completed = True
        elif (
            extraction.intent == "confirm"
            and not has_active_review
            and not session.review_rejected
            and not recovered_this_turn
            and not language_only
        ):
            reply = text("nothing_to_confirm", reply_language)
        elif pure_unrelated:
            reply = text("capability", reply_language)
        elif pending:
            reply = _first_issue(pending).question
            status = "collecting"
            summary = None
            review_revision = None
            intake_directed = True
        elif review_rejected:
            reply = text("reject_prompt", reply_language)
            status = "collecting"
            summary = None
            review_revision = None
            intake_directed = True
        elif _slots_complete(outcome.slots):
            intake_directed = True
            if outcome.accepted_fields or recovered_this_turn:
                reply = build_review_reply(outcome.slots, reply_language)
                status = "awaiting_confirmation"
                summary = build_summary(outcome.slots, reply_language)
                review_revision = new_revision
            elif current_review_valid:
                reply = build_review_reply(outcome.slots, reply_language)
                status = "awaiting_confirmation"
                summary = build_summary(outcome.slots, reply_language)
                review_revision = session.pending_review_revision
            else:
                reply = build_review_reply(outcome.slots, reply_language)
                status = "awaiting_confirmation"
                summary = build_summary(outcome.slots, reply_language)
                review_revision = new_revision
        else:
            reply = _first_missing(outcome.slots, reply_language)
            status = "collecting"
            summary = None
            review_revision = None
            intake_directed = True

        if extraction.has_question and intake_directed:
            reply = f"{reply} {text('policy_question_suffix', reply_language)}"

        return TurnPlan(
            reply=reply,
            status=status,
            summary=summary,
            review_revision=review_revision,
            citations=citations,
            completed=completed,
            review_rejected=review_rejected,
            language=reply_language,
        )

    async def _answer_policy_question(
        self, question: str, reply_language: Language
    ) -> tuple[str, tuple[Citation, ...]]:
        retrieved = self.knowledge_base.retrieve(question)
        if not retrieved:
            return text("abstention", reply_language), ()
        context = KnowledgeContext(
            question=question,
            kb_version=self.knowledge_base.version,
            sources=retrieved,
            reply_language=reply_language,
        )
        answer = await self.provider.answer_knowledge(context)
        citations = _validate_knowledge_answer(
            answer, retrieved, self.knowledge_base.version
        )
        if not answer.answerable:
            return text("abstention", reply_language), ()
        return answer.answer.strip(), citations

    async def _commit_turn(
        self,
        session: Session,
        command: TurnRequest,
        extraction: Extraction,
        outcome: MergeOutcome,
        plan: "TurnPlan",
    ) -> TurnResponse:
        now = _now()
        new_revision = session.revision + 1
        user_language: Language = (
            extraction.detected_language
            if extraction.detected_language in ("en", "es")
            else plan.language
        )
        user_message = Message(
            id=uuid4(),
            role="user",
            text=command.text,
            input_mode=command.input_mode,
            created_at=now,
            citations=[],
            language=user_language,
        )
        assistant_message = Message(
            id=uuid4(),
            role="assistant",
            text=plan.reply,
            input_mode="system",
            created_at=now,
            citations=list(plan.citations),
            language=plan.language,
        )
        response = TurnResponse(
            session_id=session.session_id,
            revision=new_revision,
            status=plan.status,
            reply_language=plan.language,
            reply=plan.reply,
            message_id=assistant_message.id,
            slots=outcome.slots,
            pending_clarifications=outcome.pending_clarifications,
            pending_review_revision=plan.review_revision,
            summary=plan.summary,
            citations=list(plan.citations),
        )
        committed = session.model_copy(deep=True)
        committed.revision = new_revision
        committed.updated_at = now
        committed.status = plan.status
        committed.reply_language = plan.language
        committed.slots = outcome.slots
        committed.pending_clarifications = outcome.pending_clarifications
        committed.pending_review_revision = plan.review_revision
        committed.summary = plan.summary
        committed.review_rejected = plan.review_rejected
        if not plan.completed:
            committed.collection_turn_count = session.collection_turn_count + 1
        if plan.completed:
            committed.confirmation = Confirmation(
                confirmed_at=now,
                review_revision=session.pending_review_revision,
            )
        committed.messages = [*session.messages, user_message, assistant_message]
        committed.requests = [
            *session.requests,
            ReplayRecord(
                request_id=command.request_id,
                input_digest=turn_digest(session.session_id, command),
                response=response,
            ),
        ]
        await self.repository.save(committed)
        return response


@dataclass(frozen=True)
class TurnPlan:
    """Result of reply selection; the transient recovery marker never persists."""

    reply: str
    status: str
    summary: str | None
    review_revision: int | None
    citations: tuple[Citation, ...]
    completed: bool
    review_rejected: bool
    language: Language


def _unsupported_plan(
    session: Session, extraction: Extraction, reply_language: Language
) -> TurnPlan:
    """Notice-only plan for input outside English and Spanish.

    Facts are never accepted: the session keeps its slots, pending issues, review
    token, summary and rejection state. A supported explicit language request is
    still applied, and the customer is invited to restate the issue.
    """

    key = (
        "language_switch_restate"
        if extraction.requested_language is not None
        else "unsupported_language"
    )
    return TurnPlan(
        reply=text(key, reply_language),
        status=session.status,
        summary=session.summary,
        review_revision=session.pending_review_revision,
        citations=(),
        completed=False,
        review_rejected=session.review_rejected,
        language=reply_language,
    )


def _has_facts(extraction: Extraction) -> bool:
    return (
        any(getattr(extraction.candidates, field) is not None for field in FIELD_ORDER)
        or bool(extraction.explicit_corrections)
        or bool(extraction.ambiguous_fields)
    )


def _is_pure_social(extraction: Extraction) -> bool:
    return extraction.intent == "social" and not _has_facts(extraction)


def _is_language_only(extraction: Extraction) -> bool:
    """An explicit language request with no facts and no question.

    Such a turn only re-renders the current state in the requested language; it
    never confirms, rejects, starts intake or accepts facts.
    """

    return (
        extraction.requested_language is not None
        and not _has_facts(extraction)
        and not extraction.has_question
        and extraction.intent != "reject"
    )


def _is_pure_capability(extraction: Extraction) -> bool:
    return extraction.intent == "capability" and not _has_facts(extraction)


def _is_pure_unrelated(extraction: Extraction) -> bool:
    return extraction.intent == "unrelated" and not _has_facts(extraction)


def _is_pure_question(extraction: Extraction) -> bool:
    return (
        extraction.has_question
        and extraction.intent == "question"
        and not _has_facts(extraction)
    )


def _current_review_valid(session: Session) -> bool:
    """Whether the stored review token still refers to the latest review.

    Unrelated questions and capability replies preserve the token while the
    revision advances (BS §5.7, IC §13.3 J08), so staleness is decided by the
    token itself, never by `pending_review_revision == revision`.
    """

    return (
        session.pending_review_revision is not None
        and session.status == "awaiting_confirmation"
        and not session.review_rejected
        and not session.pending_clarifications
        and _slots_complete(session.slots)
    )


def _apply_transitions(
    session: Session, extraction: Extraction, outcome: MergeOutcome
) -> tuple[bool, bool]:
    was_rejected = session.review_rejected
    accepted_explicit_correction = any(
        field in outcome.accepted_fields for field in extraction.explicit_corrections
    )
    unambiguous_reaffirmation = _is_unambiguous_reaffirmation(
        extraction, outcome
    )
    review_rejected = was_rejected
    recovered_this_turn = False
    if extraction.intent == "reject":
        review_rejected = was_rejected or session.pending_review_revision is not None
    elif was_rejected and (
        (accepted_explicit_correction and not extraction.has_question)
        or unambiguous_reaffirmation
    ):
        review_rejected = False
        recovered_this_turn = True
    return review_rejected, recovered_this_turn


def _is_unambiguous_reaffirmation(
    extraction: Extraction, outcome: MergeOutcome
) -> bool:
    if extraction.intent != "confirm" or extraction.has_question:
        return False
    if extraction.explicit_corrections or extraction.ambiguous_fields:
        return False
    if outcome.pending_clarifications:
        return False
    if not _slots_complete(outcome.slots):
        return False
    for field in FIELD_ORDER:
        accepted = getattr(outcome.slots, field)
        if not validate_field(field, accepted):
            return False
        raw = getattr(extraction.candidates, field)
        if raw is not None and normalize_candidate(field, raw) != accepted:
            return False
    return True


def _is_pure_confirmation(
    session: Session,
    extraction: Extraction,
    outcome: MergeOutcome,
    *,
    review_rejected: bool,
    recovered_this_turn: bool,
    current_review_valid: bool,
) -> bool:
    if recovered_this_turn or review_rejected:
        return False
    if not current_review_valid:
        return False
    if outcome.pending_clarifications:
        return False
    if extraction.intent != "confirm" or extraction.has_question:
        return False
    if extraction.explicit_corrections or extraction.ambiguous_fields:
        return False
    if outcome.slots != session.slots:
        return False
    for field in FIELD_ORDER:
        raw = getattr(extraction.candidates, field)
        if raw is not None and normalize_candidate(field, raw) != getattr(
            outcome.slots, field
        ):
            return False
    return True


def _validate_knowledge_answer(
    answer: KnowledgeAnswer,
    retrieved: list[KnowledgeRecord],
    kb_version: str,
) -> tuple[Citation, ...]:
    if not answer.answerable:
        if answer.answer != "" or answer.cited_ids:
            raise app_error("invalid_provider_response")
        return ()
    text = answer.answer.strip()
    if not 1 <= len(text) <= MAX_KNOWLEDGE_ANSWER_CHARS:
        raise app_error("invalid_provider_response")
    cited_ids = answer.cited_ids
    if not 1 <= len(cited_ids) <= 2 or len(set(cited_ids)) != len(cited_ids):
        raise app_error("invalid_provider_response")
    by_id = {record.id: record for record in retrieved}
    if any(cited_id not in by_id for cited_id in cited_ids):
        raise app_error("invalid_provider_response")
    return tuple(
        Citation(
            source_id=cited_id,
            title=by_id[cited_id].title,
            excerpt=by_id[cited_id].body,
            kb_version=kb_version,
        )
        for cited_id in cited_ids
    )
