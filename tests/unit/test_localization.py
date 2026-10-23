"""Unit tests for the reviewed English/Spanish copy layer (FEAT-02)."""

from __future__ import annotations

import pytest

from app import localization
from app.localization import (
    DEFAULT_LANGUAGE,
    STRINGS,
    SUPPORTED_LANGUAGES,
    build_review_reply,
    build_summary,
    field_label,
    text,
)
from app.prompts import (
    ABSTENTION,
    CAPABILITY,
    CAPABILITY_REPLY,
    GREETING,
    INVALID_CATEGORY,
    INVALID_DESCRIPTION,
    INVALID_ORDER,
    INVALID_URGENCY,
    LIMIT,
    MISSING_CATEGORY,
    MISSING_DESCRIPTION,
    MISSING_ORDER,
    MISSING_URGENCY,
    NO_REVIEW_TO_REJECT,
    NOTHING_TO_CONFIRM,
    POLICY_QUESTION_SUFFIX,
    RECEIPT_TEMPLATE,
    REJECT_PROMPT,
    REVIEW_SUFFIX,
    SOCIAL_ACKNOWLEDGMENT,
)
from app.schemas import Slots
from app.validation import FIELD_ORDER

SLOTS = Slots(
    order_number="NS-000123",
    category="damaged_item",
    description="My order arrived broken",
    urgency="high",
)


def test_supported_languages_are_exactly_english_and_spanish():
    assert SUPPORTED_LANGUAGES == ("en", "es")
    assert DEFAULT_LANGUAGE == "en"


def test_every_semantic_key_has_both_languages_and_no_blank_copy():
    keys = set(STRINGS)
    assert keys, "the dictionary must not be empty"
    for key, entry in STRINGS.items():
        assert set(entry) == {"en", "es"}, key
        assert entry["en"].strip(), key
        assert entry["es"].strip(), key
        assert entry["es"] != entry["en"] or key.startswith("field_"), key


def test_english_copy_is_the_single_source_for_the_existing_prompt_constants():
    assert text("greeting", "en") == GREETING
    assert text("social_acknowledgment", "en") == SOCIAL_ACKNOWLEDGMENT
    assert text("capability_reply", "en") == CAPABILITY_REPLY
    assert text("nothing_to_confirm", "en") == NOTHING_TO_CONFIRM
    assert text("no_review_to_reject", "en") == NO_REVIEW_TO_REJECT
    assert text("capability", "en") == CAPABILITY
    assert text("abstention", "en") == ABSTENTION
    assert text("receipt", "en") == RECEIPT_TEMPLATE
    assert text("limit", "en") == LIMIT
    assert text("reject_prompt", "en") == REJECT_PROMPT
    assert text("review_suffix", "en") == REVIEW_SUFFIX
    assert text("policy_question_suffix", "en") == POLICY_QUESTION_SUFFIX
    assert text("missing_order", "en") == MISSING_ORDER
    assert text("missing_category", "en") == MISSING_CATEGORY
    assert text("missing_description", "en") == MISSING_DESCRIPTION
    assert text("missing_urgency", "en") == MISSING_URGENCY
    assert text("invalid_order", "en") == INVALID_ORDER
    assert text("invalid_category", "en") == INVALID_CATEGORY
    assert text("invalid_description", "en") == INVALID_DESCRIPTION
    assert text("invalid_urgency", "en") == INVALID_URGENCY


def test_spanish_missing_and_invalid_questions_exist_for_every_field():
    for field in FIELD_ORDER:
        assert localization.missing_question(field, "es")
        assert localization.invalid_question(field, "es")
        assert localization.missing_question(field, "es") != localization.missing_question(
            field, "en"
        )


def test_field_labels_are_localized_for_every_field():
    assert [field_label(field, "es") for field in FIELD_ORDER] == [
        "número de pedido",
        "categoría del problema",
        "descripción del problema",
        "urgencia",
    ]
    assert [field_label(field, "en") for field in FIELD_ORDER] == [
        "order number",
        "problem category",
        "problem description",
        "urgency",
    ]


def test_spanish_summary_localizes_framing_but_preserves_canonical_values():
    summary = build_summary(SLOTS, "es")

    assert summary == (
        "Pedido: NS-000123\n"
        "Categoría: damaged_item\n"
        "Problema: My order arrived broken\n"
        "Urgencia: high"
    )
    assert "NS-000123" in summary
    assert "damaged_item" in summary
    assert "My order arrived broken" in summary


def test_english_summary_is_unchanged():
    assert build_summary(SLOTS, "en") == (
        "Order: NS-000123\n"
        "Category: damaged_item\n"
        "Problem: My order arrived broken\n"
        "Urgency: high"
    )


def test_review_reply_appends_the_localized_confirmation_request():
    english = build_review_reply(SLOTS, "en")
    spanish = build_review_reply(SLOTS, "es")

    assert english.endswith(REVIEW_SUFFIX)
    assert spanish.endswith(text("review_suffix", "es"))
    assert spanish.startswith(build_summary(SLOTS, "es"))


def test_conflict_and_ambiguous_templates_interpolate_the_localized_field_label():
    conflict = localization.conflict_question(
        "urgency", "low", "high", "es"
    )
    ambiguous = localization.ambiguous_question("description", "es")

    assert "low" in conflict and "high" in conflict
    assert field_label("urgency", "es") in conflict
    assert field_label("description", "es") in ambiguous


def test_unsupported_language_notice_exists_in_both_languages_and_is_localized():
    english = text("unsupported_language", "en")
    spanish = text("unsupported_language", "es")

    assert "English" in english and "Spanish" in english
    assert "inglés" in spanish and "español" in spanish
    assert english != spanish


def test_language_switch_restate_notice_exists_in_both_languages():
    assert text("language_switch_restate", "en")
    assert text("language_switch_restate", "es")
    assert text("language_switch_restate", "en") != text(
        "language_switch_restate", "es"
    )


def test_speech_unavailable_notice_is_localized():
    assert text("speech_unavailable", "es")
    assert text("speech_unavailable", "en")
    assert text("speech_unavailable", "es") != text("speech_unavailable", "en")


def test_unknown_key_or_language_fails_loudly():
    with pytest.raises(KeyError):
        text("not_a_key", "en")
    with pytest.raises(KeyError):
        text("greeting", "fr")
    with pytest.raises(KeyError):
        field_label("not_a_field", "en")
