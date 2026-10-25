"""Safety triage: a customer who may have a gas leak is told what to do first.

"I smell gas" is a statement, not a question, so the intent label alone can send it to
intake. Two independent checks catch it, and either one is enough:

* the model's ``safety_concern`` flag in the extraction;
* ``SafetyIndex``, which embeds the message and asks whether it is closer to a labelled
  emergency example or to a labelled ordinary one. The examples are sentences in a data
  file, so widening what it knows means adding a sentence, not code. It runs beside the
  extraction call, and it still works when the model is down.

A raised flag turns the turn into a policy question about gas safety. The advice itself
is the knowledge-base record, never text the model made up. Both checks lean towards
raising it: a false alarm only shows safety advice.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from gateway.knowledge.semantic import Embed, cosine
from gateway.schemas import DetectedLanguage, Extraction, Slots

# Retrieval query for a safety turn: the customer's own words may be too vague to find
# the record ("it smells funny"), so the question asked of the knowledge base is fixed.
SAFETY_QUERY = "I smell gas or suspect a gas leak"

EMBED_BATCH = 32
EMBED_DEADLINE_SECONDS = 5.0


class SafetyExamplesInvalid(Exception):
    """The examples file is missing, malformed or one-sided."""


@dataclass(frozen=True)
class SafetyExamples:
    emergency: tuple[str, ...]
    ordinary: tuple[str, ...]


def load_examples(path: Path) -> SafetyExamples:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        groups = {key: tuple(raw[key]) for key in ("emergency", "ordinary")}
    except (OSError, ValueError, KeyError, TypeError):
        raise SafetyExamplesInvalid(str(path)) from None
    everything = [text for group in groups.values() for text in group]
    if (
        not all(groups.values())
        or not all(isinstance(text, str) and text.strip() for text in everything)
        or len(set(everything)) != len(everything)
    ):
        raise SafetyExamplesInvalid(str(path))
    return SafetyExamples(**groups)


class SafetyIndex:
    """Is a message nearer to an emergency example than to an ordinary one?"""

    def __init__(self, examples: SafetyExamples, embed: Embed, *, margin: float = 0.0) -> None:
        self._examples = examples
        self._embed = embed
        self._margin = margin
        self._vectors: asyncio.Future[tuple[list[list[float]], list[list[float]]]] | None = None

    def warm(self) -> None:
        """Start embedding the examples now, so the first customer does not wait for it."""

        self._ensure()

    async def aclose(self) -> None:
        if self._vectors is not None and not self._vectors.done():
            self._vectors.cancel()

    def _ensure(self) -> asyncio.Future:
        failed = self._vectors is not None and self._vectors.done() and (
            self._vectors.cancelled() or self._vectors.exception() is not None
        )
        if self._vectors is None or failed:
            self._vectors = asyncio.ensure_future(self._embed_examples())
        return self._vectors

    async def _embed_examples(self) -> tuple[list[list[float]], list[list[float]]]:
        texts = [*self._examples.emergency, *self._examples.ordinary]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors.extend(await self._embed(texts[start : start + EMBED_BATCH]))
        split = len(self._examples.emergency)
        return vectors[:split], vectors[split:]

    async def concerning(self, message: str) -> bool:
        """True when the message looks like an emergency. Raises if embedding fails."""

        async with asyncio.timeout(EMBED_DEADLINE_SECONDS):
            # Shielded: giving up on this message must not cancel the shared warm-up.
            emergency, ordinary = await asyncio.shield(self._ensure())
            (query,) = await self._embed([message])
        return _nearest(query, emergency) > _nearest(query, ordinary) + self._margin


def _nearest(query: Sequence[float], examples: Sequence[Sequence[float]]) -> float:
    return max(cosine(query, vector) for vector in examples)


def _no_slots() -> Slots:
    return Slots(order_number=None, category=None, description=None, urgency=None)


def fallback_extraction(language: DetectedLanguage) -> Extraction:
    """What the model would have said, for a flagged turn when the model cannot answer."""

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
    """Relabel a flagged turn as a pure policy question; language and mood stay."""

    return extraction.model_copy(
        update={
            "intent": "question",
            "has_question": True,
            "candidates": _no_slots(),
            "explicit_corrections": [],
            "ambiguous_fields": [],
            "safety_concern": True,
        }
    )
