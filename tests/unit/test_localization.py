"""Unit tests for the reviewed English/Spanish copy layer."""

from __future__ import annotations

import pytest

from app import localization
from app.localization import (
    CATALOG,
    DEFAULT_LANGUAGE,
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
    GRATITUDE_ACKNOWLEDGMENT,
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
    english = CATALOG.mapping("en")
    spanish = CATALOG.mapping("es")
    assert english, "the dictionary must not be empty"
    assert set(english) == set(spanish)
    for key, value in english.items():
        assert value.strip(), key
        assert spanish[key].strip(), key
        assert spanish[key] != value or key.startswith("intake.field_"), key


def test_english_copy_is_the_single_source_for_the_existing_prompt_constants():
    assert text("greeting", "en") == GREETING
    assert text("social_acknowledgment", "en") == SOCIAL_ACKNOWLEDGMENT
    assert text("gratitude_acknowledgment", "en") == GRATITUDE_ACKNOWLEDGMENT
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
    assert localization.missing_question("order_number", "en") == MISSING_ORDER
    assert localization.missing_question("category", "en") == MISSING_CATEGORY
    assert localization.missing_question("description", "en") == MISSING_DESCRIPTION
    assert localization.missing_question("urgency", "en") == MISSING_URGENCY
    assert text("intake.invalid_order", "en") == INVALID_ORDER
    assert text("intake.invalid_category", "en") == INVALID_CATEGORY
    assert text("intake.invalid_description", "en") == INVALID_DESCRIPTION
    assert text("intake.invalid_urgency", "en") == INVALID_URGENCY


def test_welcome_greeting_is_short_and_welcoming_in_both_languages():
    assert text("greeting", "en") == (
        "Hi, welcome to Northstar Energy. How can I help you today?"
    )
    assert text("greeting", "es") == (
        "¡Hola! Te damos la bienvenida a Northstar Energy. ¿En qué puedo ayudarte hoy?"
    )


def test_gratitude_acknowledgment_is_natural_in_both_languages():
    assert text("gratitude_acknowledgment", "en") == GRATITUDE_ACKNOWLEDGMENT
    assert GRATITUDE_ACKNOWLEDGMENT == (
        "You're welcome! How else can I help with your energy service?"
    )
    assert text("gratitude_acknowledgment", "es").startswith("¡De nada!")


def test_spanish_missing_and_invalid_questions_exist_for_every_field():
    for field in FIELD_ORDER:
        assert localization.missing_question(field, "es")
        assert localization.invalid_question(field, "es")
        assert localization.missing_question(field, "es") != localization.missing_question(
            field, "en"
        )


def test_field_labels_are_localized_for_every_field():
    assert [field_label(field, "es") for field in FIELD_ORDER] == [
        "referencia de cuenta de energía",
        "área de asistencia energética",
        "descripción del problema",
        "urgencia",
    ]
    assert [field_label(field, "en") for field in FIELD_ORDER] == [
        "energy account reference",
        "energy support area",
        "problem description",
        "urgency",
    ]


def test_spanish_summary_localizes_framing_and_category_label():
    summary = build_summary(SLOTS, "es")

    assert summary == (
        "Referencia de cuenta: NS-000123\n"
        "Área de asistencia energética: problema con el equipo\n"
        "Problema: My order arrived broken\n"
        "Urgencia: high"
    )
    assert "NS-000123" in summary
    assert "problema con el equipo" in summary
    assert "My order arrived broken" in summary


def test_english_summary_is_unchanged():
    assert build_summary(SLOTS, "en") == (
        "Account reference: NS-000123\n"
        "Energy support area: equipment issue\n"
        "Issue: My order arrived broken\n"
        "Urgency: high"
    )


def test_summary_never_exposes_the_internal_category_code():
    assert "damaged_item" not in build_summary(SLOTS, "en")
    assert "damaged_item" not in build_summary(SLOTS, "es")


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
    assert text("speech.unavailable", "es")
    assert text("speech.unavailable", "en")
    assert text("speech.unavailable", "es") != text("speech.unavailable", "en")


def test_unknown_key_or_language_fails_loudly():
    with pytest.raises(KeyError):
        text("not_a_key", "en")
    with pytest.raises(KeyError):
        text("greeting", "fr")
    with pytest.raises(KeyError):
        field_label("not_a_field", "en")
