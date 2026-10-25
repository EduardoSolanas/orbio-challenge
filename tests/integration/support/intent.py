"""Shared helpers for the split test_intent_table modules."""

from __future__ import annotations

from gateway.providers.prompts import POLICY_QUESTION_SUFFIX
from tests.integration.support.api import make_extraction


SOCIAL_GREETING = "Hello, how are you?"

SOCIAL_THANKS = "Thanks."

CAPABILITY_Q = "What can you help me with?"

UNRELATED_TEXT = "Tell me a joke"

START_TEXT = "I want to report an issue"

PART_SLOTS = {
    "order_number": "NS-000123",
    "category": None,
    "description": "My order arrived broken",
    "urgency": None,
}


def social_extraction(has_question: bool = False):
    return make_extraction(intent="social", has_question=has_question)


def assert_no_policy_suffix(reply: str) -> None:
    assert POLICY_QUESTION_SUFFIX not in reply
