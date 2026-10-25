"""Conversation engine: durable turn transaction, merge and reply selection.

One repository lock spans load/dedup/check/extract/merge/save. The engine holds
no global singletons: every instance owns its repository, provider and
knowledge base, so tests isolate cleanly (IC §3-§8, RAG §5-§6).

Pure helpers live beside it: ``intent`` (turn analysis), ``plans`` (reply plans),
``replay`` (idempotency digests) and ``validation`` (slot rules).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Protocol, Sequence
from uuid import UUID, uuid4

from shared.models import OrderRecord
from gateway.backend_client import BackendApiClient
from gateway.errors import AppError, app_error
from gateway.conversation.safety import (
    SAFETY_QUERY,
    SafetyIndex,
    as_safety_question,
    fallback_extraction,
)
from gateway.conversation.intent import (
    apply_transitions,
    current_review_valid,
    is_language_only,
    is_pure_capability,
    is_pure_confirmation,
    is_pure_question,
    is_pure_social,
    is_pure_unrelated,
    slots_complete,
    transition_frustration,
)
from gateway.localization import build_review_reply, build_summary, render, text
from gateway.conversation.plans import (
    TurnPlan,
    choose_reply_language,
    first_issue,
    first_missing,
    handoff_plan,
    unchanged_outcome,
    unsupported_plan,
)
from gateway.conversation.replay import booking_digest, confirm_digest, find_replay, turn_digest
from gateway.knowledge.retrieval import validate_knowledge_answer
from gateway.schemas import (
    SESSION_SCHEMA_VERSION,
    BookingRequest,
    Citation,
    Confirmation,
    ConfirmRequest,
    Extraction,
    ExtractionContext,
    Frustration,
    KnowledgeAnswer,
    KnowledgeContext,
    KnowledgeRecord,
    Language,
    Message,
    MessageView,
    OrderVerificationRequest,
    OrderVerificationResponse,
    RecentMessage,
    ReplayRecord,
    Sentiment,
    Session,
    SessionView,
    SlotOption,
    Slots,
    TurnRequest,
    TurnResponse,
)
from gateway.knowledge.semantic import SemanticIndex
from gateway.storage import JsonSessionRepository
from gateway.conversation.validation import (
    FIELD_ORDER,
    MergeOutcome,
    merge_extraction,
    normalize_candidate,
    validate_field,
)
from gateway.conversation.verification import VERIFICATION_FAILED, VerificationRegistry
from gateway.conversation.visits import booking_offer, installation_reply, visit_text, window_values

if TYPE_CHECKING:
    from gateway.providers.client import AIProvider

__all__ = [
    "ConversationEngine",
    "KnowledgeBase",
    "build_extraction_context",
    "confirm_digest",
    "transition_frustration",
    "turn_digest",
]

ACCOUNT_INTENTS = ("order_lookup", "installation_status", "book_technician")
RECENT_MESSAGE_LIMIT = 12
MAX_COLLECTION_TURNS = 50
TEXT_TURN_DEADLINE_SECONDS = 25.0
# A flagged turn does not wait long for a model that is not answering.
SAFETY_EXTRACT_TIMEOUT_SECONDS = 8.0


class KnowledgeBase(Protocol):
    version: str

    def retrieve(self, question: str) -> list[KnowledgeRecord]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _message(
    role: Literal["user", "assistant"],
    body: str,
    *,
    input_mode: str,
    created_at: datetime,
    language: Language,
    citations: Sequence[Citation] = (),
    sentiment: Sentiment | None = None,
    frustration: Frustration | None = None,
) -> Message:
    return Message(
        id=uuid4(),
        role=role,
        text=body,
        input_mode=input_mode,
        created_at=created_at,
        citations=list(citations),
        language=language,
        sentiment=sentiment,
        frustration=frustration,
    )


def _view(session: Session, speech_languages: frozenset[str]) -> SessionView:
    return SessionView(
        session_id=session.session_id,
        revision=session.revision,
        status=session.status,
        reply_language=session.reply_language,
        frustration_active=session.frustration_active,
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


def _require_open(session: Session) -> None:
    if session.status == "handed_off":
        raise app_error("session_handed_off")
    if session.status == "completed":
        raise app_error("session_completed")


def _intake_started(session: Session) -> bool:
    """Whether this session's intake has begun, from its committed state.

    Durable ``intake_started`` is UX-01's additive field; until it exists, the
    documented inference rule decides: any accepted fact, pending issue,
    rejection, review token, summary or completion means intake has begun.
    """

    return (
        any(getattr(session.slots, field) is not None for field in FIELD_ORDER)
        or bool(session.pending_clarifications)
        or session.review_rejected
        or session.pending_review_revision is not None
        or session.summary is not None
        or session.status == "completed"
    )


def build_extraction_context(
    session: Session,
    current_message: str,
    *,
    verified_order_number: str | None = None,
) -> ExtractionContext:
    """Copy the authoritative session state into a detached extraction context.

    ``recent_messages`` is exactly the last 12 committed messages in stored
    order; the current message is supplied once, outside that list. Every nested
    object is copied so a provider cannot mutate the loaded session by keeping a
    reference (IC §13.4 "Exact model context and window algorithm").

    AUTH-01 exposes only the boolean verification state and the verified order
    number; postcode, code, email and customer first name never enter the model
    context.
    """

    return ExtractionContext(
        slots=session.slots.model_copy(),
        pending_clarifications={
            field: clarification.model_copy()
            for field, clarification in session.pending_clarifications.items()
        },
        recent_messages=[
            RecentMessage(role=message.role, text=message.text)
            for message in session.messages[-RECENT_MESSAGE_LIMIT:]
        ],
        current_message=current_message,
        status=session.status,
        intake_started=_intake_started(session),
        reply_language=session.reply_language,
        frustration_active=session.frustration_active,
        order_verified=verified_order_number is not None,
        verified_order_number=verified_order_number,
    )


class ConversationEngine:
    """Owns one session's turn transaction from load to atomic save."""

    def __init__(
        self,
        repository: JsonSessionRepository,
        provider: AIProvider,
        knowledge_base: KnowledgeBase,
        *,
        speech_languages: frozenset[str] = frozenset({"en"}),
        customer_data: BackendApiClient | None = None,
        verification: VerificationRegistry | None = None,
        semantic_index: SemanticIndex | None = None,
        safety_index: SafetyIndex | None = None,
    ) -> None:
        self.semantic_index = semantic_index
        self.safety_index = safety_index
        self.repository = repository
        self.provider = provider
        self.knowledge_base = knowledge_base
        self.speech_languages = speech_languages
        self.customer_data = customer_data
        self.verification = (
            verification if verification is not None else VerificationRegistry()
        )

    async def create_session(self) -> SessionView:
        now = _now()
        session_id = uuid4()
        session = Session(
            schema_version=SESSION_SCHEMA_VERSION,
            session_id=session_id,
            revision=0,
            created_at=now,
            updated_at=now,
            status="collecting",
            reply_language="en",
            frustration_active=False,
            slots=Slots(
                order_number=None,
                category=None,
                description=None,
                urgency=None,
            ),
            messages=[
                _message(
                    "assistant",
                    text("greeting", "en"),
                    input_mode="system",
                    created_at=now,
                    language="en",
                )
            ],
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

    async def assistant_message(self, session_id: UUID, message_id: UUID) -> Message:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            for message in session.messages:
                if message.id == message_id and message.role == "assistant":
                    return message
        raise app_error("message_not_found")

    async def assistant_text(self, session_id: UUID, message_id: UUID) -> str:
        return (await self.assistant_message(session_id, message_id)).text

    @staticmethod
    def _replayed(
        session: Session,
        request_id: UUID,
        digest: str,
        expected_revision: int,
    ) -> TurnResponse | None:
        """The stored response for a repeated request, else check the request is current.

        A repeat with the same digest returns the committed response; the same ID
        with different input conflicts. Otherwise the revision must match and the
        session must still accept input.
        """

        replay = find_replay(session, request_id)
        if replay is not None:
            if replay.input_digest != digest:
                raise app_error("request_id_conflict")
            return replay.response
        if expected_revision != session.revision:
            raise _revision_conflict(session.revision)
        _require_open(session)
        return None

    async def turn(self, session_id: UUID, command: TurnRequest) -> TurnResponse:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            replayed = self._replayed(
                session,
                command.request_id,
                turn_digest(session_id, command),
                command.expected_revision,
            )
            if replayed is not None:
                return replayed

            at_limit = session.collection_turn_count >= MAX_COLLECTION_TURNS
            if at_limit and not current_review_valid(session):
                raise app_error("turn_limit")

            try:
                async with asyncio.timeout(TEXT_TURN_DEADLINE_SECONDS):
                    extraction, outcome, plan = await self._select_plan(
                        session, command, at_limit=at_limit
                    )
            except TimeoutError:
                raise app_error("provider_timeout") from None
            return await self._commit_turn(session, command, extraction, outcome, plan)

    async def _select_plan(
        self, session: Session, command: TurnRequest, *, at_limit: bool
    ) -> tuple[Extraction, MergeOutcome, TurnPlan]:
        """Extract, merge and choose the reply for one text turn."""

        extraction = await self._extract_with_safety_triage(session, command)
        reply_language = choose_reply_language(session, extraction)

        if extraction.detected_language == "unsupported" and not extraction.safety_concern:
            if at_limit:
                raise app_error("turn_limit")
            return (
                extraction,
                unchanged_outcome(session),
                unsupported_plan(session, extraction, reply_language),
            )

        next_active, start_handoff = transition_frustration(
            session.frustration_active, extraction.frustration
        )
        if start_handoff and not extraction.safety_concern:
            return (
                extraction,
                unchanged_outcome(session),
                handoff_plan(session, reply_language),
            )

        if extraction.intent in ACCOUNT_INTENTS:
            # Account turns never start intake, accept slots or reach the
            # customer-data API without a matching grant.
            if at_limit:
                raise app_error("turn_limit")
            return (
                extraction,
                unchanged_outcome(session),
                await self._plan_order_lookup(
                    session, extraction, reply_language, next_active=next_active
                ),
            )

        outcome = merge_extraction(
            session.slots,
            session.pending_clarifications,
            extraction,
            language=reply_language,
        )
        if at_limit:
            review_rejected, recovered_this_turn = apply_transitions(
                session, extraction, outcome
            )
            if not is_pure_confirmation(
                session,
                extraction,
                outcome,
                review_rejected=review_rejected,
                recovered_this_turn=recovered_this_turn,
                current_review_valid=current_review_valid(session),
            ):
                raise app_error("turn_limit")
        plan = await self._plan_turn(
            session,
            command,
            extraction,
            outcome,
            reply_language,
            session.revision + 1,
            next_active=next_active,
        )
        return extraction, outcome, plan

    async def _embedding_concern(self, message: str) -> bool:
        """The embedding check; any failure means "no opinion", never a failed turn."""

        if self.safety_index is None:
            return False
        try:
            return await self.safety_index.concerning(message)
        except (AppError, TimeoutError):
            return False

    async def _extract_with_safety_triage(
        self, session: Session, command: TurnRequest
    ) -> Extraction:
        """Run the extraction and the embedding safety check side by side.

        A turn is a safety turn if either the embedding check or the model's own
        ``safety_concern`` flag says so. If the model cannot answer a flagged turn, the
        customer still gets the safety record instead of an error.
        """

        extraction_call = asyncio.ensure_future(
            self.provider.extract(
                build_extraction_context(
                    session,
                    command.text,
                    verified_order_number=self.verification.grant(session.session_id),
                )
            )
        )
        try:
            concern = await self._embedding_concern(command.text)
            try:
                extraction = await asyncio.wait_for(
                    extraction_call,
                    SAFETY_EXTRACT_TIMEOUT_SECONDS if concern else None,
                )
            except (AppError, TimeoutError):
                if not concern:
                    raise
                return fallback_extraction(session.reply_language)
        finally:
            if not extraction_call.done():
                extraction_call.cancel()
        if concern or extraction.safety_concern:
            return as_safety_question(extraction)
        return extraction

    async def confirm(self, session_id: UUID, command: ConfirmRequest) -> TurnResponse:
        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            digest = confirm_digest(session_id, command)
            replayed = self._replayed(
                session, command.request_id, digest, command.expected_revision
            )
            if replayed is not None:
                return replayed
            if session.pending_review_revision is None:
                # No review exists (incomplete, rejected or issue-blocked intake):
                # nothing to confirm and nothing that could be stale (IC §7, §13.2 IT04-18/19).
                raise app_error("review_not_ready")
            if command.review_revision != session.pending_review_revision:
                raise app_error("stale_review")
            if not current_review_valid(session):
                raise app_error("review_not_ready")

            now = _now()
            new_revision = session.revision + 1
            language = session.reply_language
            reply = render("receipt", language, {"session_id": session.session_id})
            assistant_message = _message(
                "assistant",
                reply,
                input_mode="system",
                created_at=now,
                language=language,
            )
            response = TurnResponse(
                session_id=session.session_id,
                revision=new_revision,
                status="completed",
                reply_language=language,
                frustration_active=session.frustration_active,
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
                _message(
                    "user",
                    text("review.confirmed", language),
                    input_mode="system",
                    created_at=now,
                    language=language,
                ),
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

    async def verify_order(
        self, session_id: UUID, command: OrderVerificationRequest
    ) -> OrderVerificationResponse:
        """Verify the pending/order-slot order without touching session JSON."""

        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            _require_open(session)
            if self.verification.locked_out(session_id):
                raise app_error("too_many_verification_attempts")
            order_number = self._pending_or_slot_order(session)
            if order_number is None or self.customer_data is None:
                self.verification.record_failure(session_id)
                return OrderVerificationResponse(
                    verified=False, error=VERIFICATION_FAILED
                )
            result = await self.customer_data.verify_order(
                order_number,
                command.delivery_postcode,
                command.verification_code,
            )
            if result.verified:
                self.verification.record_success(session_id, order_number)
            else:
                self.verification.record_failure(session_id)
            return OrderVerificationResponse(
                verified=result.verified, error=result.error
            )

    async def book_slot(
        self, session_id: UUID, command: BookingRequest
    ) -> TurnResponse:
        """Book one offered window for the verified account and record it in the chat."""

        async with self.repository.locked(session_id):
            session = await self.repository.load(session_id)
            digest = booking_digest(session_id, command)
            replayed = self._replayed(
                session, command.request_id, digest, command.expected_revision
            )
            if replayed is not None:
                return replayed
            order_number = self.verification.grant(session_id)
            if order_number is None or self.customer_data is None:
                raise app_error("verification_required")
            try:
                appointment = await self.customer_data.book(
                    order_number, command.starts_at
                )
            except AppError as error:
                # A lost response may already have booked this exact window.
                if error.code != "already_booked":
                    raise
                record = await self.customer_data.installation(order_number)
                appointment = None if record is None else record.appointment
                if appointment is None or appointment.starts_at != command.starts_at:
                    raise

            now = _now()
            language = session.reply_language
            reply = visit_text("booking.confirmed", language, appointment)
            window = window_values(appointment.starts_at, appointment.ends_at)
            assistant_message = _message(
                "assistant",
                reply,
                input_mode="system",
                created_at=now,
                language=language,
            )
            new_revision = session.revision + 1
            response = TurnResponse(
                session_id=session_id,
                revision=new_revision,
                status=session.status,
                reply_language=language,
                frustration_active=session.frustration_active,
                reply=reply,
                message_id=assistant_message.id,
                slots=session.slots,
                pending_clarifications=session.pending_clarifications,
                pending_review_revision=session.pending_review_revision,
                summary=session.summary,
            )
            committed = session.model_copy(deep=True)
            committed.revision = new_revision
            committed.updated_at = now
            committed.messages = [
                *session.messages,
                _message(
                    "user",
                    render(
                        "booking.user_choice",
                        language,
                        {"date": window["date"], "start": window["start"]},
                    ),
                    input_mode="system",
                    created_at=now,
                    language=language,
                ),
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

    def _pending_or_slot_order(self, session: Session) -> str | None:
        return (
            self.verification.pending_order(session.session_id)
            or session.slots.order_number
        )

    async def _plan_order_lookup(
        self,
        session: Session,
        extraction: Extraction,
        reply_language: Language,
        *,
        next_active: bool,
    ) -> TurnPlan:
        """Answer a lookup-only turn without intake, RAG or premature lookup.

        The customer-data API is reached only when the session holds an
        unexpired grant for this exact order and the model proposes the single
        allowlisted tool. Any other combination asks for verification or for the
        order number, revealing nothing about which detail matched.
        """

        session_id = session.session_id
        order_number = self._lookup_order_number(session, extraction)
        required = False
        slot_options: tuple[SlotOption, ...] = ()
        if extraction.candidates.order_number is not None and order_number is None:
            reply = text("intake.invalid_order", reply_language)
        elif order_number is None:
            reply = text("auth.order_number_prompt", reply_language)
        elif self.verification.grant(session_id) != order_number:
            self.verification.set_pending_order(session_id, order_number)
            reply = text("auth.verification_prompt", reply_language)
            required = True
        elif extraction.intent == "installation_status":
            reply = await installation_reply(
                self.customer_data, order_number, reply_language
            )
        elif extraction.intent == "book_technician":
            reply, slot_options = await booking_offer(
                self.customer_data, order_number, reply_language
            )
        elif extraction.tool_request is not None:
            order = await self.customer_data.lookup_order(order_number)
            if order is None:
                self.verification.clear(session_id)
                reply = text("auth.verification_failed", reply_language)
            else:
                reply = self._order_summary(order, reply_language)
        else:
            reply = text("auth.verified", reply_language)

        return TurnPlan.keeping_state(
            session,
            reply,
            reply_language,
            frustration_active=next_active,
            order_verification_required=required,
            slot_options=slot_options,
        )

    def _lookup_order_number(
        self, session: Session, extraction: Extraction
    ) -> str | None:
        """Validated order for this lookup: new candidate, pending, or slot."""

        candidate = extraction.candidates.order_number
        if candidate is not None:
            normalized = normalize_candidate("order_number", candidate)
            if normalized is not None and validate_field("order_number", normalized):
                return normalized
            return None
        return self._pending_or_slot_order(session)

    @staticmethod
    def _order_summary(order: OrderRecord, language: Language) -> str:
        """Render the allowlisted fixture fields with no extra customer data."""

        return render(
            "auth.order_result",
            language,
            {
                "order_number": order.order_number,
                "first_name": order.first_name,
                "item_names": ", ".join(order.item_names),
                "status": text(f"auth.status_{order.status}", language),
            },
        )

    async def _plan_turn(
        self,
        session: Session,
        command: TurnRequest,
        extraction: Extraction,
        outcome: MergeOutcome,
        reply_language: Language,
        new_revision: int,
        *,
        next_active: bool,
    ) -> TurnPlan:
        pending = outcome.pending_clarifications
        review_rejected, recovered_this_turn = apply_transitions(
            session, extraction, outcome
        )
        review_valid = current_review_valid(session)
        has_active_review = session.pending_review_revision is not None
        language_only = is_language_only(extraction)
        pure_social = is_pure_social(extraction) and not language_only
        pure_capability = is_pure_capability(extraction) and not language_only
        pure_unrelated = is_pure_unrelated(extraction) and not language_only
        pure_question = is_pure_question(extraction)
        pure_confirmation = (
            is_pure_confirmation(
                session,
                extraction,
                outcome,
                review_rejected=review_rejected,
                recovered_this_turn=recovered_this_turn,
                current_review_valid=review_valid,
            )
            and not language_only
        )

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
            reply = text(
                "gratitude_acknowledgment"
                if extraction.intent == "gratitude"
                else "social_acknowledgment",
                reply_language,
            )
        elif pure_capability:
            reply = text("capability_reply", reply_language)
        elif pure_question:
            reply, citations = await self._answer_policy_question(
                SAFETY_QUERY if extraction.safety_concern else command.text,
                reply_language,
                urgent=extraction.safety_concern,
            )
        elif pure_confirmation:
            reply = render(
                "receipt", reply_language, {"session_id": session.session_id}
            )
            status = "completed"
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
            reply = first_issue(pending).question
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
        elif slots_complete(outcome.slots):
            intake_directed = True
            reply = build_review_reply(outcome.slots, reply_language)
            status = "awaiting_confirmation"
            summary = build_summary(outcome.slots, reply_language)
            unchanged_valid_review = (
                review_valid and not outcome.accepted_fields and not recovered_this_turn
            )
            review_revision = (
                session.pending_review_revision
                if unchanged_valid_review
                else new_revision
            )
        else:
            reply = first_missing(outcome.slots, reply_language)
            status = "collecting"
            summary = None
            review_revision = None
            intake_directed = True

        if extraction.has_question and intake_directed:
            reply = f"{reply} {text('policy_question_suffix', reply_language)}"

        pending_order = self.verification.pending_order(session.session_id)
        verification_required = (
            pending_order is not None
            and self.verification.grant(session.session_id) != pending_order
        )

        return TurnPlan(
            reply=reply,
            status=status,
            summary=summary,
            review_revision=review_revision,
            citations=citations,
            completed=completed,
            review_rejected=review_rejected,
            language=reply_language,
            frustration_active=next_active,
            order_verification_required=verification_required,
        )

    async def _retrieve(self, question: str) -> list[KnowledgeRecord]:
        """Semantic retrieval when configured, keyword retrieval if it is unavailable."""

        if self.semantic_index is not None:
            try:
                return await self.semantic_index.retrieve(question)
            except (AppError, TimeoutError):
                pass
        return self.knowledge_base.retrieve(question)

    async def _answer_policy_question(
        self, question: str, reply_language: Language, *, urgent: bool = False
    ) -> tuple[str, tuple[Citation, ...]]:
        retrieved = await self._retrieve(question)
        if not retrieved:
            return text("abstention", reply_language), ()
        context = KnowledgeContext(
            question=question,
            kb_version=self.knowledge_base.version,
            sources=retrieved,
            reply_language=reply_language,
        )
        try:
            answer = await self.provider.answer_knowledge(context)
        except AppError:
            if not urgent:
                raise
            # A safety answer must not depend on the model: give the record itself.
            answer = KnowledgeAnswer(
                answerable=True, answer=retrieved[0].body, cited_ids=[retrieved[0].id]
            )
        citations = validate_knowledge_answer(
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
        plan: TurnPlan,
    ) -> TurnResponse:
        now = _now()
        new_revision = session.revision + 1
        unsupported = extraction.detected_language == "unsupported"
        user_language: Language = (
            extraction.detected_language
            if extraction.detected_language in ("en", "es")
            else plan.language
        )
        user_message = _message(
            "user",
            command.text,
            input_mode=command.input_mode,
            created_at=now,
            language=user_language,
            sentiment="uncertain" if unsupported else extraction.sentiment,
            frustration="uncertain" if unsupported else extraction.frustration,
        )
        assistant_message = _message(
            "assistant",
            plan.reply,
            input_mode="system",
            created_at=now,
            language=plan.language,
            citations=plan.citations,
        )
        response = TurnResponse(
            session_id=session.session_id,
            revision=new_revision,
            status=plan.status,
            reply_language=plan.language,
            frustration_active=plan.frustration_active,
            reply=plan.reply,
            message_id=assistant_message.id,
            slots=outcome.slots,
            pending_clarifications=outcome.pending_clarifications,
            pending_review_revision=plan.review_revision,
            summary=plan.summary,
            citations=list(plan.citations),
            order_verification_required=plan.order_verification_required,
            slot_options=list(plan.slot_options),
        )
        if session.slots.order_number != outcome.slots.order_number:
            # An accepted order change invalidates any grant for the old order;
            # the failure count survives so a lockout cannot be reset this way.
            # Accepting the already verified order into intake keeps its grant.
            if self.verification.grant(session.session_id) != (
                outcome.slots.order_number
            ):
                self.verification.clear_grant(session.session_id)
        committed = session.model_copy(deep=True)
        committed.revision = new_revision
        committed.updated_at = now
        committed.status = plan.status
        committed.reply_language = plan.language
        committed.frustration_active = plan.frustration_active
        committed.slots = outcome.slots
        committed.pending_clarifications = outcome.pending_clarifications
        committed.pending_review_revision = plan.review_revision
        committed.summary = plan.summary
        committed.review_rejected = plan.review_rejected
        if plan.completed:
            committed.confirmation = Confirmation(
                confirmed_at=now,
                review_revision=session.pending_review_revision,
            )
        else:
            committed.collection_turn_count = session.collection_turn_count + 1
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
        if plan.status == "handed_off":
            self.verification.clear(session.session_id)
        return response
