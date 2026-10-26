"""The gas-safety embedding check: examples live in data, the decision is a nearest-example vote."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gateway.conversation import safety
from gateway.conversation.safety import (
    SAFETY_QUERY,
    SafetyExamples,
    SafetyExamplesInvalid,
    SafetyIndex,
    as_safety_question,
    fallback_extraction,
    load_examples,
)
from tests.support.unit_fakes import make_extraction

ROOT = Path(__file__).resolve().parents[4]
SHIPPED = ROOT / "gateway" / "knowledge" / "safety-examples.json"
EVAL_CASES = json.loads((ROOT / "tests" / "eval" / "utterances.json").read_text("utf-8"))

EXAMPLES = SafetyExamples(emergency=("gas smell here",), ordinary=("my bill is high",))


def keyword_embed():
    """Messages mentioning a smell point one way, everything else the other."""

    calls: list[list[str]] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.1] if "smell" in text.lower() else [0.1, 1.0] for text in texts]

    embed.calls = calls
    return embed


# --- the data file ---------------------------------------------------------------


def test_the_shipped_examples_load_and_cover_both_languages() -> None:
    examples = load_examples(SHIPPED)

    assert len(examples.emergency) >= 15 and len(examples.ordinary) >= 15
    assert any("gas" in text.lower() for text in examples.emergency)
    assert any("gas" in text.lower() and "huele" in text.lower() for text in examples.emergency)


def test_no_example_is_an_evaluation_message() -> None:
    """Otherwise the evaluation would mark its own homework."""

    examples = load_examples(SHIPPED)
    used = {
        case["text"].casefold()
        for key in ("safety", "intents", "category", "frustration", "retrieval", "retrieval_holdout")
        for case in EVAL_CASES[key]
    }

    assert not [t for t in (*examples.emergency, *examples.ordinary) if t.casefold() in used]


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{not json",
        {"emergency": ["a"]},
        {"emergency": [], "ordinary": ["a"]},
        {"emergency": ["a"], "ordinary": ["a"]},
        {"emergency": ["a", "a"], "ordinary": ["b"]},
        {"emergency": ["  "], "ordinary": ["b"]},
        {"emergency": [1], "ordinary": ["b"]},
    ],
)
def test_a_missing_malformed_or_one_sided_file_is_rejected(tmp_path: Path, content) -> None:
    path = tmp_path / "examples.json"
    if content is not None:
        path.write_text(content if isinstance(content, str) else json.dumps(content), "utf-8")

    with pytest.raises(SafetyExamplesInvalid):
        load_examples(path)


# --- the index -------------------------------------------------------------------


async def test_a_message_nearer_an_emergency_example_is_flagged() -> None:
    index = SafetyIndex(EXAMPLES, keyword_embed())

    assert await index.concerning("something smells odd") is True
    assert await index.concerning("what is my balance") is False


async def test_the_examples_are_embedded_once_however_many_messages_arrive() -> None:
    embed = keyword_embed()
    index = SafetyIndex(EXAMPLES, embed)

    await index.concerning("a smell")
    await index.concerning("my balance")

    assert embed.calls[0] == ["gas smell here", "my bill is high"]
    assert [len(call) for call in embed.calls[1:]] == [1, 1]


async def test_many_examples_are_embedded_in_bounded_batches() -> None:
    embed = keyword_embed()
    many = SafetyExamples(
        emergency=tuple(f"smell {n}" for n in range(40)),
        ordinary=tuple(f"bill {n}" for n in range(40)),
    )

    await SafetyIndex(many, embed).concerning("smell")

    assert max(len(call) for call in embed.calls) <= safety.EMBED_BATCH
    assert sum(len(call) for call in embed.calls[:-1]) == 80


async def test_a_failed_warm_up_is_retried_on_the_next_message() -> None:
    attempts = []
    good = keyword_embed()

    async def flaky(texts: list[str]) -> list[list[float]]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("embedding service down")
        return await good(texts)

    index = SafetyIndex(EXAMPLES, flaky)

    with pytest.raises(RuntimeError):
        await index.concerning("a smell")
    assert await index.concerning("a smell") is True


async def test_a_slow_embedding_service_gives_up_instead_of_holding_the_turn(monkeypatch) -> None:
    monkeypatch.setattr(safety, "EMBED_DEADLINE_SECONDS", 0.05)

    async def hang(texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(5)
        return []

    with pytest.raises(TimeoutError):
        await SafetyIndex(EXAMPLES, hang).concerning("a smell")


async def test_the_margin_makes_the_check_less_eager() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        # The message sits a little nearer the emergency example than the ordinary one.
        return [
            {"gas smell here": [1.0, 0.0], "my bill is high": [0.0, 1.0]}.get(text, [0.6, 0.5])
            for text in texts
        ]

    assert await SafetyIndex(EXAMPLES, embed).concerning("ambiguous") is True
    assert await SafetyIndex(EXAMPLES, embed, margin=0.5).concerning("ambiguous") is False


async def test_warm_up_starts_early_and_close_cancels_it() -> None:
    started = asyncio.Event()

    async def slow(texts: list[str]) -> list[list[float]]:
        started.set()
        await asyncio.sleep(5)
        return []

    index = SafetyIndex(EXAMPLES, slow)
    index.warm()
    await asyncio.wait_for(started.wait(), 1)
    await index.aclose()


# --- relabelling -----------------------------------------------------------------


def test_a_flagged_turn_becomes_a_pure_question_and_keeps_language_and_mood() -> None:
    original = make_extraction(
        intent="start_intake",
        candidates={"description": "smell", "urgency": "high"},
        detected_language="es",
        frustration="present",
    )

    relabelled = as_safety_question(original)

    assert relabelled.intent == "question" and relabelled.has_question
    assert relabelled.safety_concern is True
    assert relabelled.candidates.description is None and relabelled.candidates.urgency is None
    assert relabelled.detected_language == "es"
    assert relabelled.frustration == "present"
    assert original.intent == "start_intake" and original.safety_concern is False


def test_the_fallback_when_the_model_is_down_is_a_flagged_question_in_the_session_language() -> None:
    extraction = fallback_extraction("es")

    assert extraction.intent == "question" and extraction.has_question
    assert extraction.safety_concern is True
    assert extraction.detected_language == "es"
    assert extraction.frustration == "none"


def test_the_extraction_defaults_to_no_concern() -> None:
    assert make_extraction().safety_concern is False


def test_the_fixed_retrieval_query_names_the_hazard() -> None:
    assert "gas" in SAFETY_QUERY and "leak" in SAFETY_QUERY
