"""Pure normalization, field validation and slot merge rules (IC §5).

No I/O, no clock, no session mutation: every helper takes plain values and
returns new values so ``conversation.py`` can call them inside a turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from app.prompts import (
    AMBIGUOUS_TEMPLATE,
    CONFLICT_TEMPLATE,
    FIELD_LABELS,
    INVALID_QUESTION_BY_FIELD,
    MISSING_QUESTION_BY_FIELD,
    REJECT_PROMPT,
    build_review_reply,
)
from app.schemas import Clarification, Extraction, FieldName, Slots

FIELD_ORDER: tuple[FieldName, ...] = (
    "order_number",
    "category",
    "description",
    "urgency",
)

ORDER_NUMBER_PATTERN = re.compile(r"(?i)(?:NS-)?[0-9]{6}")

CATEGORY_ALIAS_TO_CANONICAL: dict[str, str] = {
    "delivery": "delivery",
    "late delivery": "delivery",
    "late package": "delivery",
    "missing package": "delivery",
    "not delivered": "delivery",
    "damaged_item": "damaged_item",
    "damaged item": "damaged_item",
    "damaged": "damaged_item",
    "broken item": "damaged_item",
    "wrong_item": "wrong_item",
    "wrong item": "wrong_item",
    "incorrect item": "wrong_item",
    "return_refund": "return_refund",
    "return": "return_refund",
    "refund": "return_refund",
    "return or refund": "return_refund",
    "other": "other",
}

URGENCY_ALIAS_TO_CANONICAL: dict[str, str] = {
    "low": "low",
    "low priority": "low",
    "not urgent": "low",
    "normal": "normal",
    "normal priority": "normal",
    "medium": "normal",
    "high": "high",
    "high priority": "high",
    "urgent": "high",
}

CATEGORY_CANONICAL = frozenset(CATEGORY_ALIAS_TO_CANONICAL.values())
URGENCY_CANONICAL = frozenset(URGENCY_ALIAS_TO_CANONICAL.values())


@dataclass(frozen=True)
class MergeOutcome:
    """Accepted slots plus the pending issues after one extraction merge."""

    slots: Slots
    pending_clarifications: dict[FieldName, Clarification]
    accepted_fields: frozenset[FieldName]
    cleared_fields: frozenset[FieldName]


def normalize_candidate(field: FieldName, candidate: str | None) -> str | None:
    if candidate is None:
        return None
    value = candidate.strip()
    if field == "order_number":
        if ORDER_NUMBER_PATTERN.fullmatch(value):
            return f"NS-{value[-6:]}"
        return value
    if field in ("category", "urgency"):
        collapsed = re.sub(r"\s+", " ", value.lower())
        table = (
            CATEGORY_ALIAS_TO_CANONICAL
            if field == "category"
            else URGENCY_ALIAS_TO_CANONICAL
        )
        return table.get(collapsed, value)
    return value


def validate_field(field: FieldName, value: str) -> bool:
    value = value.strip()
    if field == "order_number":
        return (
            ORDER_NUMBER_PATTERN.fullmatch(value) is not None
            and value == f"NS-{value[-6:]}"
        )
    if field == "category":
        return value in CATEGORY_CANONICAL
    if field == "urgency":
        return value in URGENCY_CANONICAL
    if field == "description":
        if not 10 <= len(value) <= 2000:
            return False
        collapsed = re.sub(r"\s+", " ", value.lower())
        return collapsed not in CATEGORY_ALIAS_TO_CANONICAL
    raise NotImplementedError


def merge_extraction(
    slots: Slots,
    pending_clarifications: Mapping[FieldName, Clarification],
    extraction: Extraction,
) -> MergeOutcome:
    merged = slots.model_copy()
    pending: dict[FieldName, Clarification] = {}
    accepted: set[FieldName] = set()
    cleared: set[FieldName] = set()
    for field in FIELD_ORDER:
        existing = pending_clarifications.get(field)
        raw = getattr(extraction.candidates, field)
        if field in extraction.ambiguous_fields:
            candidate = normalize_candidate(field, raw)
            pending[field] = Clarification(
                code="ambiguous",
                candidate=candidate,
                question=AMBIGUOUS_TEMPLATE.format_map(
                    {"field label": FIELD_LABELS[field]}
                ),
            )
            continue
        if raw is None:
            if existing is not None:
                pending[field] = existing
            continue
        candidate = normalize_candidate(field, raw)
        if not validate_field(field, candidate):
            pending[field] = Clarification(
                code="invalid",
                candidate=candidate,
                question=INVALID_QUESTION_BY_FIELD[field],
            )
            continue
        old = getattr(merged, field)
        if old is None or candidate == old:
            setattr(merged, field, candidate)
            accepted.add(field)
            if existing is not None:
                cleared.add(field)
        elif field in extraction.explicit_corrections:
            setattr(merged, field, candidate)
            accepted.add(field)
            if existing is not None:
                cleared.add(field)
        else:
            pending[field] = Clarification(
                code="conflict",
                candidate=candidate,
                question=CONFLICT_TEMPLATE.format_map(
                    {
                        "old": old,
                        "candidate": candidate,
                        "field label": FIELD_LABELS[field],
                    }
                ),
            )
    return MergeOutcome(
        slots=merged,
        pending_clarifications=pending,
        accepted_fields=frozenset(accepted),
        cleared_fields=frozenset(cleared),
    )


def select_reply(
    *,
    new_reject: bool,
    pending_clarifications: Mapping[FieldName, Clarification],
    review_rejected: bool,
    slots: Slots,
) -> str:
    if new_reject:
        return REJECT_PROMPT
    for field in FIELD_ORDER:
        clarification = pending_clarifications.get(field)
        if clarification is not None:
            return clarification.question
    if review_rejected:
        return REJECT_PROMPT
    for field in FIELD_ORDER:
        if getattr(slots, field) is None:
            return MISSING_QUESTION_BY_FIELD[field]
    return build_review_reply(slots)

