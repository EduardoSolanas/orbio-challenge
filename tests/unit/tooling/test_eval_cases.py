"""The labelled utterances behind scripts/evaluate.py stay well formed."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest

from gateway.knowledge.retrieval import load_knowledge
from gateway.schemas import Intent

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads((ROOT / "tests" / "eval" / "utterances.json").read_text("utf-8"))
RECORD_IDS = {
    r.id
    for r in load_knowledge(
        ROOT / "gateway" / "knowledge" / "northstar-faq.json"
    ).records
}
# Context-free intents only: confirm, reject and correction need an open review.
EVALUATED = set(get_args(Intent)) - {"confirm", "reject", "correction"}


def test_every_context_free_intent_has_english_and_spanish_utterances() -> None:
    counts = Counter((c["intent"], c["language"]) for c in CASES["intents"])

    for intent in EVALUATED:
        assert counts[(intent, "en")] >= 5, intent
        assert counts[(intent, "es")] >= 3, intent


def test_intent_labels_are_real_intents_and_utterances_are_unique() -> None:
    texts = [c["text"] for c in CASES["intents"]]

    assert {c["intent"] for c in CASES["intents"]} == EVALUATED
    assert len(texts) == len(set(texts))


@pytest.mark.parametrize("key", ["retrieval", "retrieval_holdout"])
def test_retrieval_cases_name_real_records_and_include_abstentions(key: str) -> None:
    expected = [c["record"] for c in CASES[key]]

    assert {r for r in expected if r is not None} == RECORD_IDS
    assert expected.count(None) >= 3


def test_the_held_out_questions_are_not_in_the_tuning_set() -> None:
    tuning = {c["text"] for c in CASES["retrieval"]}

    assert not tuning & {c["text"] for c in CASES["retrieval_holdout"]}


def test_frustration_cases_cover_angry_and_calm_messages_in_both_languages() -> None:
    cases = CASES["frustration"]
    counts = Counter((c["frustration"], c["language"]) for c in cases)

    assert {c["frustration"] for c in cases} == {"present", "none"}
    for label in ("present", "none"):
        assert counts[(label, "en")] >= 4
        assert counts[(label, "es")] >= 2
    assert len({c["text"] for c in cases}) == len(cases)


def test_safety_cases_have_both_labels_in_both_languages_and_are_unique() -> None:
    cases = CASES["safety"]
    texts = [c["text"] for c in cases]

    assert len(texts) == len(set(texts))
    assert all(isinstance(c["concern"], bool) for c in cases)
    for flag in (True, False):
        for language in ("en", "es"):
            assert (
                sum(
                    1
                    for c in cases
                    if c["concern"] is flag and c["language"] == language
                )
                >= 4
            )


def test_category_cases_cover_every_stored_category_in_both_languages() -> None:
    cases = CASES["category"]
    texts = [c["text"] for c in cases]

    assert len(texts) == len(set(texts))
    assert {c["category"] for c in cases} == {
        "delivery",
        "damaged_item",
        "wrong_item",
        "return_refund",
        "other",
    }
    for category in {c["category"] for c in cases}:
        assert (
            sum(1 for c in cases if c["category"] == category and c["language"] == "en")
            >= 3
        ), category
        assert (
            sum(1 for c in cases if c["category"] == category and c["language"] == "es")
            >= 2
        ), category
