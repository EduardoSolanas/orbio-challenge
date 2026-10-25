"""Reply plans: what a turn will say and which session fields it will change.

A plan is the result of reply selection. The transient recovery marker never
persists; ``ConversationEngine`` copies a plan into the committed session.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.localization import missing_question, text
from app.schemas import (
    Citation,
    Clarification,
    Extraction,
    FieldName,
    Language,
    Session,
    SlotOption,
    Slots,
)
from app.validation import FIELD_ORDER, MergeOutcome


@dataclass(frozen=True)
class TurnPlan:
    reply: str
    status: str
    summary: str | None
    review_revision: int | None
    citations: tuple[Citation, ...]
    completed: bool
    review_rejected: bool
    language: Language
    frustration_active: bool
    order_verification_required: bool = False
    slot_options: tuple[SlotOption, ...] = ()

    @classmethod
    def keeping_state(
        cls,
        session: Session,
        reply: str,
        language: Language,
        *,
        frustration_active: bool,
        order_verification_required: bool = False,
        slot_options: tuple[SlotOption, ...] = (),
    ) -> TurnPlan:
        """A reply that leaves status, summary, review token and rejection as they are."""

        return cls(
            reply=reply,
            status=session.status,
            summary=session.summary,
            review_revision=session.pending_review_revision,
            citations=(),
            completed=False,
            review_rejected=session.review_rejected,
            language=language,
            frustration_active=frustration_active,
            order_verification_required=order_verification_required,
            slot_options=slot_options,
        )


def handoff_plan(session: Session, language: Language) -> TurnPlan:
    """Fixed, honest, demo-only simulated handoff message."""

    return TurnPlan(
        reply=text("frustration_handoff", language),
        status="handed_off",
        summary=session.summary,
        review_revision=session.pending_review_revision,
        citations=(),
        completed=False,
        review_rejected=session.review_rejected,
        language=language,
        frustration_active=True,
    )


def unsupported_plan(
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
    return TurnPlan.keeping_state(
        session,
        text(key, reply_language),
        reply_language,
        frustration_active=session.frustration_active,
    )


def choose_reply_language(session: Session, extraction: Extraction) -> Language:
    """An explicit requested language wins; otherwise a supported detection does."""

    if extraction.requested_language is not None:
        return extraction.requested_language
    if extraction.detected_language in ("en", "es"):
        return extraction.detected_language
    return session.reply_language


def unchanged_outcome(session: Session) -> MergeOutcome:
    """Outcome that accepts nothing, for input the demo cannot understand."""

    return MergeOutcome(
        slots=session.slots,
        pending_clarifications=session.pending_clarifications,
        accepted_fields=frozenset(),
        cleared_fields=frozenset(),
    )


def first_issue(pending: dict[FieldName, Clarification]) -> Clarification | None:
    for field in FIELD_ORDER:
        clarification = pending.get(field)
        if clarification is not None:
            return clarification
    return None


def first_missing(slots: Slots, language: Language) -> str | None:
    for field in FIELD_ORDER:
        if getattr(slots, field) is None:
            return missing_question(field, language)
    return None
