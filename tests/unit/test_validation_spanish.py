"""Unit tests for Spanish canonical aliases and localized clarifications (FEAT-02)."""

from __future__ import annotations

import pytest

from app.localization import field_label
from app.schemas import Clarification, Extraction, Slots
from app.validation import merge_extraction, normalize_candidate, validate_field

NO_SLOTS = {
    "order_number": None,
    "category": None,
    "description": None,
    "urgency": None,
}


def make_slots(**overrides) -> Slots:
    return Slots(**{**NO_SLOTS, **overrides})


def make_extraction(
    candidates: dict | None = None,
    *,
    intent: str = "provide_details",
    explicit_corrections: list[str] | None = None,
    ambiguous_fields: list[str] | None = None,
    detected_language: str = "es",
    requested_language: str | None = None,
) -> Extraction:
    return Extraction(
        intent=intent,
        has_question=False,
        candidates=make_slots(**(candidates or {})),
        explicit_corrections=explicit_corrections or [],
        ambiguous_fields=ambiguous_fields or [],
        detected_language=detected_language,
        requested_language=requested_language,
        sentiment="neutral",
        frustration="none",
    )


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("entrega", "delivery"),
        ("Entrega", "delivery"),
        ("envío", "delivery"),
        ("envio", "delivery"),
        ("no entregado", "delivery"),
        ("artículo dañado", "damaged_item"),
        ("articulo danado", "damaged_item"),
        ("Artículo Dañado", "damaged_item"),
        ("roto", "damaged_item"),
        ("artículo incorrecto", "wrong_item"),
        ("producto incorrecto", "wrong_item"),
        ("devolución", "return_refund"),
        ("devolucion", "return_refund"),
        ("reembolso", "return_refund"),
        ("otro", "other"),
        ("otra cosa", "other"),
    ],
)
def test_spanish_category_aliases_normalize_to_canonical_values(raw, canonical):
    assert normalize_candidate("category", raw) == canonical
    assert validate_field("category", canonical)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("baja", "low"),
        ("Baja", "low"),
        ("normal", "normal"),
        ("media", "normal"),
        ("alta", "high"),
        ("urgente", "high"),
    ],
)
def test_spanish_urgency_aliases_normalize_to_canonical_values(raw, canonical):
    assert normalize_candidate("urgency", raw) == canonical
    assert validate_field("urgency", canonical)


def test_unknown_spanish_word_stays_verbatim_and_is_not_a_canonical_category():
    assert normalize_candidate("category", "paquete misterioso") == "paquete misterioso"
    assert not validate_field("category", "paquete misterioso")


def test_spanish_order_number_preserves_leading_zeros():
    assert normalize_candidate("order_number", "123456") == "NS-123456"
    assert normalize_candidate("order_number", "NS-000042") == "NS-000042"
    assert validate_field("order_number", "NS-000042")


def test_description_rejects_a_bare_spanish_category_label():
    assert not validate_field("description", "entrega")
    assert not validate_field("description", "dañado")
    assert validate_field("description", "Mi pedido llegó dañado y faltan piezas")


def test_spanish_invalid_question_is_localized_in_the_merge_outcome():
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction(candidates={"category": "paquete misterioso"}),
        language="es",
    )

    clarification = outcome.pending_clarifications["category"]
    assert clarification.code == "invalid"
    assert clarification.question == (
        "Elige entrega, artículo dañado, artículo incorrecto, "
        "devolución o reembolso, u otro."
    )
    assert clarification.candidate == "paquete misterioso"


def test_spanish_conflict_question_localizes_framing_and_keeps_values():
    outcome = merge_extraction(
        make_slots(urgency="low"),
        {},
        make_extraction(candidates={"urgency": "alta"}),
        language="es",
    )

    clarification = outcome.pending_clarifications["urgency"]
    assert clarification.code == "conflict"
    assert "low" in clarification.question
    assert "high" in clarification.question
    assert field_label("urgency", "es") in clarification.question


def test_spanish_ambiguous_question_localizes_the_field_label():
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction(
            candidates={"description": "algo"}, ambiguous_fields=["description"]
        ),
        language="es",
    )

    assert outcome.pending_clarifications["description"].question == (
        "Aclara tu descripción del problema."
    )


def test_merge_defaults_to_english_copy_when_no_language_is_given():
    outcome = merge_extraction(
        make_slots(), {}, make_extraction(candidates={"category": "paquete misterioso"}, detected_language="en")
    )

    assert outcome.pending_clarifications["category"].question == (
        "Please choose delivery, damaged item, wrong item, return/refund, or other."
    )


def test_spanish_correction_replaces_the_english_accepted_value():
    outcome = merge_extraction(
        make_slots(category="delivery"),
        {},
        make_extraction(
            candidates={"category": "artículo dañado"}, explicit_corrections=["category"]
        ),
        language="es",
    )

    assert outcome.slots.category == "damaged_item"
    assert outcome.pending_clarifications == {}
