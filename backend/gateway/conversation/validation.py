"""Pure normalization, field validation and slot merge rules (IC §5).

No I/O, no clock, no session mutation: every helper takes plain values and
returns new values so ``conversation.py`` can call them inside a turn.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from backend.gateway.localization import (
    ambiguous_question,
    build_review_reply,
    conflict_question,
    invalid_question,
    missing_question,
    text,
)
from backend.gateway.schemas import Clarification, Extraction, FieldName, Language, Slots

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
    "entrega": "delivery",
    "entregas": "delivery",
    "envio": "delivery",
    "envios": "delivery",
    "enviar": "delivery",
    "no entregado": "delivery",
    "paquete perdido": "delivery",
    "retraso": "delivery",
    "damaged_item": "damaged_item",
    "damaged item": "damaged_item",
    "damaged": "damaged_item",
    "broken item": "damaged_item",
    "articulo danado": "damaged_item",
    "articulo danada": "damaged_item",
    "producto danado": "damaged_item",
    "danado": "damaged_item",
    "danada": "damaged_item",
    "roto": "damaged_item",
    "rota": "damaged_item",
    "rotos": "damaged_item",
    "rotas": "damaged_item",
    "wrong_item": "wrong_item",
    "wrong item": "wrong_item",
    "incorrect item": "wrong_item",
    "articulo incorrecto": "wrong_item",
    "producto incorrecto": "wrong_item",
    "articulo equivocado": "wrong_item",
    "equivocado": "wrong_item",
    "return_refund": "return_refund",
    "return": "return_refund",
    "refund": "return_refund",
    "return or refund": "return_refund",
    "devolucion": "return_refund",
    "devoluciones": "return_refund",
    "devolver": "return_refund",
    "reembolso": "return_refund",
    "reembolsos": "return_refund",
    "reembolsar": "return_refund",
    "other": "other",
    "otro": "other",
    "otra": "other",
    "otra cosa": "other",
}

URGENCY_ALIAS_TO_CANONICAL: dict[str, str] = {
    "low": "low",
    "low priority": "low",
    "not urgent": "low",
    "baja": "low",
    "bajo": "low",
    "no urgente": "low",
    "normal": "normal",
    "normal priority": "normal",
    "medium": "normal",
    "media": "normal",
    "medio": "normal",
    "high": "high",
    "high priority": "high",
    "urgent": "high",
    "alta": "high",
    "alto": "high",
    "urgente": "high",
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


def _fold(value: str) -> str:
    """Accent-insensitive, case-insensitive lookup key for alias tables."""

    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped.casefold()).strip()


def normalize_candidate(field: FieldName, candidate: str | None) -> str | None:
    if candidate is None:
        return None
    value = candidate.strip()
    if field == "order_number":
        if ORDER_NUMBER_PATTERN.fullmatch(value):
            return f"NS-{value[-6:]}"
        return value
    if field in ("category", "urgency"):
        table = (
            CATEGORY_ALIAS_TO_CANONICAL
            if field == "category"
            else URGENCY_ALIAS_TO_CANONICAL
        )
        return table.get(_fold(value), value)
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
        return _fold(value) not in CATEGORY_ALIAS_TO_CANONICAL
    raise NotImplementedError


def merge_extraction(
    slots: Slots,
    pending_clarifications: Mapping[FieldName, Clarification],
    extraction: Extraction,
    language: Language = "en",
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
                question=ambiguous_question(field, language),
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
                question=invalid_question(field, language),
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
                question=conflict_question(field, old, candidate, language),
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
    language: Language = "en",
) -> str:
    if new_reject:
        return text("reject_prompt", language)
    for field in FIELD_ORDER:
        clarification = pending_clarifications.get(field)
        if clarification is not None:
            return clarification.question
    if review_rejected:
        return text("reject_prompt", language)
    for field in FIELD_ORDER:
        if getattr(slots, field) is None:
            return missing_question(field, language)
    return build_review_reply(slots, language)
