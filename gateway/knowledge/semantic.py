"""Semantic retrieval: cosine similarity between embedded records and the question.

The records are embedded once, on first use, and kept in memory. A record is used
only when its similarity reaches the configured floor, so an unrelated question
retrieves nothing and the assistant abstains. Any embedding failure raises, and
the engine falls back to keyword retrieval for that question.
"""

from __future__ import annotations

import asyncio
import math
from typing import Awaitable, Callable, Mapping, Sequence

from gateway.errors import app_error
from gateway.knowledge.retrieval import TOP_K
from gateway.schemas import KnowledgeRecord

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
EMBED_DEADLINE_SECONDS = 5.0


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise app_error("invalid_provider_response")
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    if norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / norm


class SemanticIndex:
    def __init__(
        self,
        records: Sequence[KnowledgeRecord],
        embed: Embed,
        *,
        min_similarity: float,
        alternates: Mapping[str, str] | None = None,
    ) -> None:
        """``alternates`` maps a record id to a second text for it (its Spanish version).

        A record scores as its better match, so a question in either language finds it.
        """

        self._records = tuple(records)
        self._embed = embed
        self._min_similarity = min_similarity
        self._alternates = {
            record.id: alternates[record.id]
            for record in self._records
            if alternates and record.id in alternates
        }
        self._vectors: list[list[float]] | None = None

    async def retrieve(self, question: str) -> list[KnowledgeRecord]:
        """The best ``TOP_K`` records at or above the similarity floor."""

        async with asyncio.timeout(EMBED_DEADLINE_SECONDS):
            if self._vectors is None:
                self._vectors = await self._embed(
                    [f"{record.title}. {record.body}" for record in self._records]
                    + list(self._alternates.values())
                )
            (query,) = await self._embed([question])
        # Vectors come in record order, then the alternates in the order they were given.
        extra = dict(zip(self._alternates, self._vectors[len(self._records) :]))
        ranked = sorted(
            (
                (
                    max(
                        cosine(query, vector),
                        cosine(query, extra[record.id]) if record.id in extra else -1.0,
                    ),
                    record.id,
                    record,
                )
                for vector, record in zip(self._vectors, self._records)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return [
            record
            for score, _, record in ranked[:TOP_K]
            if score >= self._min_similarity
        ]
