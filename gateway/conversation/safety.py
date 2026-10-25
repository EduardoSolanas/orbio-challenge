"""Safety triage: a customer who may have a gas leak is told what to do first.

"I smell gas" is a statement, not a question, so the intent label alone can send it to
intake. Two checks catch it, either of which is enough:

* a small rule on the raw message, run *before* the model is called, so it still works
  when the model is slow or down. It only knows the obvious wording;
* the extraction's ``safety_concern`` flag, set by the model for everything the rule
  cannot know ("my kitchen smells funny"), tuned to err on the side of raising it.

A raised flag turns the turn into a policy question about gas safety. The advice
itself is the knowledge-base record, never text the model made up. Over-matching only
costs a safe answer.
"""

from __future__ import annotations

import re

from gateway.schemas import DetectedLanguage, Extraction, Slots

# Retrieval query for a safety turn: the customer's own words may be too vague to find
# the record ("it smells funny"), so the question asked of the knowledge base is fixed.
SAFETY_QUERY = "I smell gas or suspect a gas leak"

_ENGLISH = re.compile(
    r"\b(?:smell|smells|smelt|smelling)\b[^.?!]{0,30}\bgas\b"
    r"|\bgas\b[^.?!]{0,20}\b(?:smell|smells|leak|leaks|leaking)\b",
    re.IGNORECASE,
)
_SPANISH = re.compile(
    r"\b(?:huele|huelo|olor)\b[^.?!]{0,20}\bgas\b"
    r"|\b(?:fuga|escape)\b[^.?!]{0,10}\bgas\b"
    r"|\bgas\b[^.?!]{0,20}\b(?:fuga|escape)\b",
    re.IGNORECASE,
)


def rule_language(message: str) -> DetectedLanguage | None:
    """The language of an obvious gas-smell report, or None when the rule does not fire."""

    if _SPANISH.search(message):
        return "es"
    if _ENGLISH.search(message):
        return "en"
    return None


def _no_slots() -> Slots:
    return Slots(order_number=None, category=None, description=None, urgency=None)


def rule_extraction(language: DetectedLanguage) -> Extraction:
    """What the model would have said, for a turn the rule caught before any model call."""

    return Extraction(
        intent="question",
        has_question=True,
        candidates=_no_slots(),
        explicit_corrections=[],
        ambiguous_fields=[],
        detected_language=language,
        requested_language=None,
        sentiment="uncertain",
        frustration="none",
        safety_concern=True,
    )


def as_safety_question(extraction: Extraction) -> Extraction:
    """Relabel a turn the model flagged as a pure policy question; language and mood stay."""

    return extraction.model_copy(
        update={
            "intent": "question",
            "has_question": True,
            "candidates": _no_slots(),
            "explicit_corrections": [],
            "ambiguous_fields": [],
        }
    )
