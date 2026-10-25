"""Semantic retrieval through the real app, route and engine.

Only the embedding function is scripted: it places the question near the record
the test names. The knowledge base, retrieval ranking, citation checks and
fallback are the real implementations.
"""

from __future__ import annotations

from pathlib import Path

from app.errors import app_error
from app.main import DEFAULT_KNOWLEDGE_PATH, create_app
from app.prompts import ABSTENTION
from app.retrieval import load_knowledge
from app.semantic import SemanticIndex
from tests.integration.support.api import running_client
from tests.integration.support.intake import post_turn, start_session
from tests.integration.support.rag import RETURN_Q, grounded, question_extraction

RECORDS = load_knowledge(Path(DEFAULT_KNOWLEDGE_PATH)).records
METER_Q = "My power meter has a cracked casing, what should I do?"
ANSWER = "Report the damage and keep the item as it is."


def embed_near(record_id: str | None):
    """Records are one-hot by position; the question points at ``record_id`` (or nowhere)."""

    async def embed(texts: list[str]) -> list[list[float]]:
        size = len(RECORDS)
        if len(texts) == size:
            return [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]
        index = next((i for i, r in enumerate(RECORDS) if r.id == record_id), None)
        return [[0.0] * size if index is None else [1.0 if i == index else 0.0 for i in range(size)]]

    return embed


def app_with(settings, provider, embed):
    index = SemanticIndex(RECORDS, embed, min_similarity=0.5)
    return create_app(settings=settings, provider=provider, semantic_index=index)


def sources_of(provider) -> list[str]:
    (context,) = [c for name, c in provider.calls if name == "answer_knowledge"]
    return [record.id for record in context.sources]


async def test_the_semantically_closest_record_is_retrieved_and_cited(settings, provider):
    async with running_client(app_with(settings, provider, embed_near("faq-damaged"))) as client:
        session_id = await start_session(client)
        provider.script_answer(grounded(ANSWER, ["faq-damaged"]))

        _, body = await post_turn(
            client, provider, session_id, text=METER_Q, revision=0, extraction=question_extraction()
        )

    assert sources_of(provider) == ["faq-damaged"]
    assert [citation["source_id"] for citation in body["citations"]] == ["faq-damaged"]


async def test_a_question_close_to_nothing_abstains_without_calling_the_model(settings, provider):
    async with running_client(app_with(settings, provider, embed_near(None))) as client:
        session_id = await start_session(client)

        _, body = await post_turn(
            client, provider, session_id, text=METER_Q, revision=0, extraction=question_extraction()
        )

    assert body["reply"].startswith(ABSTENTION)
    assert body["citations"] == []
    assert "answer_knowledge" not in provider.methods()


async def test_an_embedding_outage_falls_back_to_keyword_retrieval(settings, provider):
    async def down(texts: list[str]) -> list[list[float]]:
        raise app_error("provider_unavailable")

    async with running_client(app_with(settings, provider, down)) as client:
        session_id = await start_session(client)
        provider.script_answer(grounded(ANSWER, ["faq-returns"]))

        _, body = await post_turn(
            client, provider, session_id, text=RETURN_Q, revision=0, extraction=question_extraction()
        )

    assert "faq-returns" in sources_of(provider)
    assert [citation["source_id"] for citation in body["citations"]] == ["faq-returns"]
