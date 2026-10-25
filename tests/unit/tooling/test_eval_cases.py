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
RECORD_IDS = {r.id for r in load_knowledge(ROOT / "gateway" / "knowledge" / "northstar-faq.json").records}
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
