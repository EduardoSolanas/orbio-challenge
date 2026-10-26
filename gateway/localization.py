"""Shared English/Spanish translation catalog loaded from ``app/locales``.

``app/locales/en.json`` and ``app/locales/es.json`` are the only authored
customer-facing dictionaries, shared by the backend and the browser. Keys are
flat semantic names; both files must ship identical key and named-placeholder
sets. The catalog is validated once per lifespan and exposed as immutable
mappings; unknown keys, languages or placeholder arguments fail loudly instead
of rendering a raw key or mixing languages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from gateway.schemas import FieldName, Language, Slots

SUPPORTED_LANGUAGES: tuple[Language, ...] = ("en", "es")
DEFAULT_LANGUAGE: Language = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LocalizationInvalid(Exception):
    """A shipped dictionary is missing, malformed or inconsistent."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise LocalizationInvalid(f"duplicate translation key: {key}")
        document[key] = value
    return document


def _read_dictionary(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LocalizationInvalid(f"unreadable dictionary: {path.name}") from exc
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise LocalizationInvalid(f"malformed dictionary: {path.name}") from exc
    if not isinstance(document, dict):
        raise LocalizationInvalid(f"dictionary must be a JSON object: {path.name}")
    for key, value in document.items():
        if not isinstance(key, str) or not key.strip():
            raise LocalizationInvalid(f"blank translation key in {path.name}")
        if not isinstance(value, str) or not value.strip():
            raise LocalizationInvalid(f"translation {key} must be a non-empty string")
    return document


def _placeholders(template: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER.findall(template))


def _check_language(language: str) -> Language:
    if language not in SUPPORTED_LANGUAGES:
        raise KeyError(language)
    return language


@dataclass(frozen=True)
class LocaleCatalog:
    """Validated, immutable dictionaries for every supported language."""

    _dictionaries: Mapping[str, Mapping[str, str]]

    @property
    def languages(self) -> tuple[Language, ...]:
        return SUPPORTED_LANGUAGES

    def mapping(self, language: str) -> Mapping[str, str]:
        return self._dictionaries[_check_language(language)]

    def text(self, key: str, language: str = DEFAULT_LANGUAGE) -> str:
        return self.mapping(language)[key]

    def render(self, key: str, language: str, values: Mapping[str, object]) -> str:
        template = self.text(key, language)
        names = _placeholders(template)
        if names != set(values):
            raise ValueError(f"placeholder mismatch for {key}")
        return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)


def load_locales(directory: Path | None = None) -> LocaleCatalog:
    """Load and validate both dictionaries from ``directory`` (package default)."""

    base = LOCALES_DIR if directory is None else Path(directory)
    dictionaries = {
        language: _read_dictionary(base / f"{language}.json")
        for language in SUPPORTED_LANGUAGES
    }
    reference = dictionaries[DEFAULT_LANGUAGE]
    for language, table in dictionaries.items():
        if set(table) != set(reference):
            raise LocalizationInvalid(
                f"key mismatch for {language}: {sorted(set(table) ^ set(reference))}"
            )
        for key, template in table.items():
            if _placeholders(template) != _placeholders(reference[key]):
                raise LocalizationInvalid(f"placeholder mismatch for {language}: {key}")
    return LocaleCatalog(
        MappingProxyType(
            {
                language: MappingProxyType(dict(table))
                for language, table in dictionaries.items()
            }
        )
    )


CATALOG = load_locales()

MISSING_QUESTION_KEYS: dict[FieldName, str] = {
    "order_number": "intake.order_number",
    "category": "intake.category",
    "description": "intake.description",
    "urgency": "intake.urgency",
}

MISSING_QUESTION_HINT_KEYS: dict[FieldName, str] = {
    "order_number": "intake.order_number_hint",
}

INVALID_QUESTION_KEYS: dict[FieldName, str] = {
    "order_number": "intake.invalid_order",
    "category": "intake.invalid_category",
    "description": "intake.invalid_description",
    "urgency": "intake.invalid_urgency",
}

FIELD_LABEL_KEYS: dict[FieldName, str] = {
    "order_number": "intake.field_order_number",
    "category": "intake.field_category",
    "description": "intake.field_description",
    "urgency": "intake.field_urgency",
}

CATEGORY_LABEL_KEYS: dict[str, str] = {
    "delivery": "category.supply_issue",
    "damaged_item": "category.equipment_issue",
    "wrong_item": "category.meter_reading",
    "return_refund": "category.billing",
    "other": "category.other_energy_issue",
}


def text(key: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Return the reviewed copy for ``key`` in ``language``."""

    return CATALOG.text(key, language)


def render(key: str, language: str, values: Mapping[str, object]) -> str:
    """Substitute named placeholders with exactly the supplied argument set."""

    return CATALOG.render(key, language, values)


def field_label(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return text(FIELD_LABEL_KEYS[field], language)


def missing_question(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    base = text(MISSING_QUESTION_KEYS[field], language)
    hint_key = MISSING_QUESTION_HINT_KEYS.get(field)
    if hint_key is None:
        return base
    return f"{base} {text(hint_key, language)}"


def invalid_question(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return text(INVALID_QUESTION_KEYS[field], language)


def conflict_question(
    field: FieldName, old: str, candidate: str, language: str = DEFAULT_LANGUAGE
) -> str:
    return render(
        "intake.conflict",
        language,
        {
            "old": old,
            "candidate": candidate,
            "field_label": field_label(field, language),
        },
    )


def ambiguous_question(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return render(
        "intake.ambiguous", language, {"field_label": field_label(field, language)}
    )


def build_summary(slots: Slots, language: str = DEFAULT_LANGUAGE) -> str:
    values = {field: getattr(slots, field) for field in MISSING_QUESTION_KEYS}
    if any(value is None for value in values.values()):
        raise ValueError("summary requires all four fields")
    category_key = CATEGORY_LABEL_KEYS.get(str(values["category"]))
    if category_key is None:
        raise ValueError("summary requires a canonical support category")
    values["category"] = text(category_key, language)
    return render("summary", language, values)


def build_review_reply(slots: Slots, language: str = DEFAULT_LANGUAGE) -> str:
    return build_summary(slots, language) + text("review_suffix", language)


def request_reference(session_id: object) -> str:
    """The short reference shown on a receipt: REQ- and the first eight characters of the id."""

    return "REQ-" + str(session_id).replace("-", "")[:8].upper()
