"""Pure turn analysis: what one extraction means for the session's review state.

Every helper takes plain values and returns plain values: no I/O, no clock and no
session mutation. ``conversation.py`` combines these answers into a reply plan
(IC §5, BS §5).
"""

from __future__ import annotations

from gateway.conversation.validation import (
    FIELD_ORDER,
    MergeOutcome,
    normalize_candidate,
    validate_field,
)
from gateway.schemas import Extraction, Frustration, Session, Slots


def slots_complete(slots: Slots) -> bool:
    return all(getattr(slots, field) is not None for field in FIELD_ORDER)


def transition_frustration(
    previous_active: bool, label: Frustration
) -> tuple[bool, bool]:
    """Return ``(next_active, start_handoff)`` for the episode transition table."""

    if label == "present":
        return True, not previous_active
    if label == "uncertain":
        return previous_active, False
    return False, False


def has_facts(extraction: Extraction) -> bool:
    return (
        any(getattr(extraction.candidates, field) is not None for field in FIELD_ORDER)
        or bool(extraction.explicit_corrections)
        or bool(extraction.ambiguous_fields)
    )


def is_pure_social(extraction: Extraction) -> bool:
    return extraction.intent in {"social", "gratitude"} and not has_facts(extraction)


def is_language_only(extraction: Extraction) -> bool:
    """An explicit language request with no facts and no question.

    Such a turn only re-renders the current state in the requested language; it
    never confirms, rejects, starts intake or accepts facts.
    """

    return (
        extraction.requested_language is not None
        and not has_facts(extraction)
        and not extraction.has_question
        and extraction.intent != "reject"
    )


def is_pure_capability(extraction: Extraction) -> bool:
    return extraction.intent == "capability" and not has_facts(extraction)


def is_pure_unrelated(extraction: Extraction) -> bool:
    return extraction.intent == "unrelated" and not has_facts(extraction)


def is_pure_question(extraction: Extraction) -> bool:
    return (
        extraction.has_question
        and extraction.intent == "question"
        and not has_facts(extraction)
    )


def current_review_valid(session: Session) -> bool:
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
        and slots_complete(session.slots)
    )


def apply_transitions(
    session: Session, extraction: Extraction, outcome: MergeOutcome
) -> tuple[bool, bool]:
    """Return ``(review_rejected, recovered_this_turn)`` after this extraction."""

    was_rejected = session.review_rejected
    accepted_explicit_correction = any(
        field in outcome.accepted_fields for field in extraction.explicit_corrections
    )
    review_rejected = was_rejected
    recovered_this_turn = False
    if extraction.intent == "reject":
        review_rejected = was_rejected or session.pending_review_revision is not None
    elif was_rejected and (
        (accepted_explicit_correction and not extraction.has_question)
        or is_unambiguous_reaffirmation(extraction, outcome)
    ):
        review_rejected = False
        recovered_this_turn = True
    return review_rejected, recovered_this_turn


def _candidates_match(extraction: Extraction, slots: Slots) -> bool:
    """Every supplied candidate normalizes to the value already accepted."""

    for field in FIELD_ORDER:
        raw = getattr(extraction.candidates, field)
        if raw is not None and normalize_candidate(field, raw) != getattr(slots, field):
            return False
    return True


def is_unambiguous_reaffirmation(extraction: Extraction, outcome: MergeOutcome) -> bool:
    if extraction.intent != "confirm" or extraction.has_question:
        return False
    if extraction.explicit_corrections or extraction.ambiguous_fields:
        return False
    if outcome.pending_clarifications or not slots_complete(outcome.slots):
        return False
    if not all(
        validate_field(field, getattr(outcome.slots, field)) for field in FIELD_ORDER
    ):
        return False
    return _candidates_match(extraction, outcome.slots)


def is_pure_confirmation(
    session: Session,
    extraction: Extraction,
    outcome: MergeOutcome,
    *,
    review_rejected: bool,
    recovered_this_turn: bool,
    current_review_valid: bool,
) -> bool:
    if recovered_this_turn or review_rejected or not current_review_valid:
        return False
    if outcome.pending_clarifications:
        return False
    if extraction.intent != "confirm" or extraction.has_question:
        return False
    if extraction.explicit_corrections or extraction.ambiguous_fields:
        return False
    if outcome.slots != session.slots:
        return False
    return _candidates_match(extraction, outcome.slots)
