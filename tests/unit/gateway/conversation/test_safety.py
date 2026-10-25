"""The gas-smell rule: obvious wording is caught in code, everything else is left to the model."""

from __future__ import annotations

import pytest

from gateway.conversation.safety import (
    SAFETY_QUERY,
    as_safety_question,
    rule_extraction,
    rule_language,
)
from tests.support.unit_fakes import make_extraction


@pytest.mark.parametrize(
    "message",
    [
        "i smell gas",
        "I can smell gas in my kitchen",
        "There is a smell of gas near the meter",
        "gas smell in the hallway",
        "I think there is a gas leak",
        "the gas is leaking",
    ],
)
def test_obvious_english_reports_are_caught(message: str) -> None:
    assert rule_language(message) == "en"


@pytest.mark.parametrize(
    "message",
    [
        "Huelo a gas en la cocina",
        "hay olor a gas",
        "Hay una fuga de gas",
        "creo que hay un escape de gas",
    ],
)
def test_obvious_spanish_reports_are_caught(message: str) -> None:
    assert rule_language(message) == "es"


@pytest.mark.parametrize(
    "message",
    [
        "my power is out",
        "what is a standing charge",
        "I want to report a problem with my meter",
        "gas bill is too high",
        "Mi factura de gas es alta",
        "hello",
        # Left to the model's flag: no gas word, so no rule can know.
        "my kitchen smells funny",
    ],
)
def test_other_messages_are_left_to_the_model(message: str) -> None:
    assert rule_language(message) is None


def test_a_rule_hit_is_a_pure_flagged_question_in_its_language() -> None:
    extraction = rule_extraction("es")

    assert extraction.intent == "question" and extraction.has_question
    assert extraction.safety_concern is True
    assert extraction.detected_language == "es"
    assert extraction.candidates.order_number is None
    assert extraction.frustration == "none"


def test_a_model_flag_turns_the_turn_into_a_question_and_keeps_language_and_mood() -> None:
    original = make_extraction(
        intent="start_intake",
        candidates={"description": "smell", "urgency": "high"},
        detected_language="es",
        frustration="present",
        safety_concern=True,
    )

    relabelled = as_safety_question(original)

    assert relabelled.intent == "question" and relabelled.has_question
    assert relabelled.candidates.description is None and relabelled.candidates.urgency is None
    assert relabelled.detected_language == "es"
    assert relabelled.frustration == "present"
    assert relabelled.safety_concern is True
    assert original.intent == "start_intake"


def test_the_extraction_defaults_to_no_concern() -> None:
    assert make_extraction().safety_concern is False


def test_the_fixed_retrieval_query_names_the_hazard() -> None:
    assert "gas" in SAFETY_QUERY and "leak" in SAFETY_QUERY
