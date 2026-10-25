"""Unit tests for the shared English/Spanish translation catalog.

The shipped dictionaries are the single authored source for backend and browser
copy: flat semantic keys, identical key and placeholder sets, immutable runtime
mappings and strict named-placeholder substitution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.schemas import Slots
from gateway.localization import (
    CATALOG,
    DEFAULT_LANGUAGE,
    LOCALES_DIR,
    SUPPORTED_LANGUAGES,
    LocalizationInvalid,
    load_locales,
    render,
    text,
)


def write_locales(
    directory: Path, english: dict[str, object], spanish: dict[str, object]
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "en.json").write_text(
        json.dumps(english, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "es.json").write_text(
        json.dumps(spanish, ensure_ascii=False), encoding="utf-8"
    )
    return directory


def test_supported_languages_and_default_are_exactly_english_and_spanish():
    assert SUPPORTED_LANGUAGES == ("en", "es")
    assert DEFAULT_LANGUAGE == "en"


def test_shipped_dictionaries_load_from_the_package_locale_directory():
    catalog = load_locales()
    assert catalog.languages == ("en", "es")
    assert (LOCALES_DIR / "en.json").is_file()
    assert (LOCALES_DIR / "es.json").is_file()


def test_key_sets_are_identical_between_both_languages():
    english = CATALOG.mapping("en")
    spanish = CATALOG.mapping("es")
    assert english, "the catalog must not be empty"
    assert set(english) == set(spanish)


def test_placeholder_sets_are_identical_between_both_languages():
    english = CATALOG.mapping("en")
    spanish = CATALOG.mapping("es")
    for key, template in english.items():
        assert _placeholders(template) == _placeholders(spanish[key]), key


def _placeholders(template: str) -> set[str]:
    import re

    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template))


def test_every_value_is_a_nonempty_utf8_string():
    for language in SUPPORTED_LANGUAGES:
        for key, value in CATALOG.mapping(language).items():
            assert isinstance(value, str), key
            assert value.strip(), (language, key)


def test_required_semantic_keys_are_shipped_in_both_languages():
    required = {
        "greeting",
        "actions.send",
        "actions.new_intake",
        "actions.retry",
        "speech.play",
        "speech.loading",
        "speech.unavailable",
        "speech.disclosure",
        "intake.order_number",
        "errors.provider_unavailable",
        "errors.internal_error",
        "ui.message_label",
        "ui.sources",
    }
    for language in SUPPORTED_LANGUAGES:
        assert required <= set(CATALOG.mapping(language))


def test_spanish_copy_keeps_utf8_special_characters():
    spanish = CATALOG.mapping("es")
    assert "¡Hola!" in spanish["greeting"]
    assert "¿" in spanish["intake.order_number"]
    assert "referencia" in spanish["intake.field_order_number"]
    assert "energía" in spanish["intake.field_order_number"]


def test_unknown_key_and_unknown_language_raise_key_error():
    with pytest.raises(KeyError):
        text("not_a_key", "en")
    with pytest.raises(KeyError):
        text("greeting", "fr")
    with pytest.raises(KeyError):
        CATALOG.mapping("fr")


def test_mapping_is_immutable():
    mapping = CATALOG.mapping("en")
    with pytest.raises(TypeError):
        mapping["greeting"] = "changed"  # type: ignore[index]


def test_render_substitutes_named_placeholders_as_plain_text():
    assert render("speech.play", "en", {"ordinal": 2}) == "Play reply 2"
    assert render("speech.play", "es", {"ordinal": 2}) == "Reproducir respuesta 2"
    assert (
        render("ui.knowledge_base_version", "es", {"version": "2026-10-22"})
        == "Versión de la base de conocimiento: 2026-10-22"
    )
    assert (
        render("receipt", "en", {"session_id": "abc"})
        == "Saved locally as abc. No request has been sent to a real support team."
    )


def test_render_rejects_missing_and_extra_arguments():
    with pytest.raises(ValueError):
        render("speech.play", "en", {})
    with pytest.raises(ValueError):
        render("speech.play", "en", {"ordinal": 1, "other": 2})
    with pytest.raises(ValueError):
        render("greeting", "en", {"ordinal": 1})


def test_render_treats_braces_inside_values_as_literal_text():
    rendered = render("receipt", "en", {"session_id": "{other.__class__}"})
    assert "{other.__class__}" in rendered
    assert "Saved locally as {other.__class__}" in rendered


def test_duplicate_keys_are_rejected(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").write_text(
        '{"greeting": "One", "greeting": "Two"}', encoding="utf-8"
    )
    (directory / "es.json").write_text('{"greeting": "Uno"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_malformed_json_is_rejected(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").write_text("{not json", encoding="utf-8")
    (directory / "es.json").write_text('{"greeting": "Uno"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_missing_file_is_rejected(tmp_path: Path):
    directory = write_locales(tmp_path / "locales", {"greeting": "Hello"}, {"greeting": "Hola"})
    (directory / "es.json").unlink()

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_missing_key_in_one_language_is_rejected(tmp_path: Path):
    directory = write_locales(
        tmp_path / "locales",
        {"greeting": "Hello", "actions.send": "Send"},
        {"greeting": "Hola"},
    )

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_placeholder_mismatch_is_rejected(tmp_path: Path):
    directory = write_locales(
        tmp_path / "locales",
        {"speech.play": "Play reply {ordinal}"},
        {"speech.play": "Reproducir respuesta"},
    )

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_placeholder_name_mismatch_is_rejected(tmp_path: Path):
    directory = write_locales(
        tmp_path / "locales",
        {"speech.play": "Play reply {ordinal}"},
        {"speech.play": "Reproducir respuesta {numero}"},
    )

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


@pytest.mark.parametrize("value", [1, None, ["a"], {"a": "b"}])
def test_non_string_values_are_rejected(tmp_path: Path, value: object):
    directory = write_locales(tmp_path / "locales", {"greeting": value}, {"greeting": "Hola"})

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_values_are_rejected(tmp_path: Path, value: str):
    directory = write_locales(tmp_path / "locales", {"greeting": value}, {"greeting": "Hola"})

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_non_object_document_is_rejected(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").write_text('["greeting"]', encoding="utf-8")
    (directory / "es.json").write_text('{"greeting": "Hola"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid):
        load_locales(directory)


def test_duplicate_key_error_names_the_offending_key(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").write_text('{"greeting": "One", "greeting": "Two"}', encoding="utf-8")
    (directory / "es.json").write_text('{"greeting": "Uno"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "duplicate translation key: greeting"


def test_unreadable_dictionary_names_the_file(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").mkdir()
    (directory / "es.json").write_text('{"greeting": "Uno"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "unreadable dictionary: en.json"


def test_malformed_dictionary_error_names_the_file(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").write_text("{not json", encoding="utf-8")
    (directory / "es.json").write_text('{"greeting": "Uno"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "malformed dictionary: en.json"


def test_non_object_document_error_names_the_file(tmp_path: Path):
    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "en.json").write_text('["greeting"]', encoding="utf-8")
    (directory / "es.json").write_text('{"greeting": "Uno"}', encoding="utf-8")

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "dictionary must be a JSON object: en.json"


def test_blank_translation_key_is_rejected(tmp_path: Path):
    directory = write_locales(tmp_path / "locales", {"": "Hello"}, {"greeting": "Hola"})

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "blank translation key in en.json"


def test_blank_value_error_names_the_offending_key(tmp_path: Path):
    directory = write_locales(tmp_path / "locales", {"greeting": "  "}, {"greeting": "Hola"})

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "translation greeting must be a non-empty string"


def test_key_mismatch_error_names_the_missing_key(tmp_path: Path):
    directory = write_locales(
        tmp_path / "locales",
        {"greeting": "Hello", "actions.send": "Send"},
        {"greeting": "Hola"},
    )

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "key mismatch for es: ['actions.send']"


def test_placeholder_mismatch_error_names_the_key(tmp_path: Path):
    directory = write_locales(
        tmp_path / "locales",
        {"speech.play": "Play reply {ordinal}"},
        {"speech.play": "Reproducir respuesta"},
    )

    with pytest.raises(LocalizationInvalid) as caught:
        load_locales(directory)

    assert str(caught.value) == "placeholder mismatch for es: speech.play"


def test_unknown_language_key_error_carries_the_requested_language():
    with pytest.raises(KeyError) as caught:
        CATALOG.mapping("fr")

    assert caught.value.args == ("fr",)


def test_render_mismatch_error_names_the_key():
    with pytest.raises(ValueError) as caught:
        render("speech.play", "en", {})

    assert str(caught.value) == "placeholder mismatch for speech.play"


def test_order_question_hint_is_localized_with_the_requested_language():
    from gateway.localization import missing_question

    assert missing_question("order_number", "en") == (
        "What is your energy account reference? Use NS- followed by six digits, for example NS-123456."
    )
    assert missing_question("order_number", "es") == (
        "¿Cuál es tu referencia de cuenta de energía? Usa NS- seguido de seis dígitos, por ejemplo NS-123456."
    )
    assert missing_question("urgency", "es") == "¿Qué urgencia tiene: baja, normal o alta?"


def test_incomplete_summary_error_is_explicit():
    from gateway.localization import build_summary

    with pytest.raises(ValueError) as caught:
        build_summary(
            Slots.model_construct(
                order_number="NS-000123",
                category=None,
                description=None,
                urgency=None,
            )
        )

    assert str(caught.value) == "summary requires all four fields"
