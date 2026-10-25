"""Unit tests for semantic retrieval, the embeddings wire parser and the adapter call."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gateway.config import ServiceProfile
from gateway.errors import AppError
from gateway.providers.wire import InvalidContent, parse_embeddings
from gateway.providers.client import OpenAICompatibleProvider
from gateway.schemas import KnowledgeRecord
from gateway.knowledge.semantic import SemanticIndex, cosine
from tests.unit.gateway.providers.provider_support import (
    LLM_PROFILE,
    RAG_PROFILE,
    STT_PROFILE,
    TTS_PROFILE,
    Clock,
    RoleTransport,
    SleepRecorder,
    json_response,
    unexpected,
)

RECORDS = [
    KnowledgeRecord(id="faq-a", title="A", keywords=["a"], body="alpha"),
    KnowledgeRecord(id="faq-b", title="B", keywords=["b"], body="beta"),
    KnowledgeRecord(id="faq-c", title="C", keywords=["c"], body="gamma"),
]
VECTORS = {"A. alpha": [1.0, 0.0], "B. beta": [0.0, 1.0], "C. gamma": [0.7, 0.7]}


def fake_embed(query_vector: list[float], calls: list[list[str]] | None = None):
    async def embed(texts: list[str]) -> list[list[float]]:
        if calls is not None:
            calls.append(texts)
        if texts[0] in VECTORS:
            return [VECTORS[text] for text in texts]
        return [query_vector]

    return embed


def ids(records) -> list[str]:
    return [record.id for record in records]


def test_cosine_is_scale_free_and_zero_for_a_zero_vector() -> None:
    assert cosine([1, 0], [5, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 3]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 1]) == 0.0


def test_cosine_rejects_vectors_of_different_size() -> None:
    with pytest.raises(AppError) as error:
        cosine([1, 0], [1, 0, 0])
    assert error.value.code == "invalid_provider_response"


async def test_the_two_closest_records_are_returned_best_first() -> None:
    index = SemanticIndex(RECORDS, fake_embed([1.0, 0.1]), min_similarity=0.1)

    assert ids(await index.retrieve("question")) == ["faq-a", "faq-c"]


async def test_records_below_the_floor_are_dropped_so_the_assistant_abstains() -> None:
    index = SemanticIndex(RECORDS, fake_embed([1.0, 0.0]), min_similarity=0.9)

    assert ids(await index.retrieve("question")) == ["faq-a"]
    unrelated = SemanticIndex(RECORDS, fake_embed([-1.0, -1.0]), min_similarity=0.1)
    assert await unrelated.retrieve("question") == []


async def test_records_are_embedded_once_and_each_question_once() -> None:
    calls: list[list[str]] = []
    index = SemanticIndex(RECORDS, fake_embed([1.0, 0.0], calls), min_similarity=0.1)

    await index.retrieve("one")
    await index.retrieve("two")

    assert [len(texts) for texts in calls] == [3, 1, 1]


async def test_a_failed_first_attempt_is_retried_on_the_next_question() -> None:
    attempts = {"count": 0}

    async def flaky(texts: list[str]) -> list[list[float]]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise AppError("provider_unavailable", "down", 503, True)
        return await fake_embed([1.0, 0.0])(texts)

    index = SemanticIndex(RECORDS, flaky, min_similarity=0.1)

    with pytest.raises(AppError):
        await index.retrieve("question")
    assert ids(await index.retrieve("question")) == ["faq-a", "faq-c"]


async def test_a_slow_embedding_service_times_out() -> None:
    async def slow(texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(1)
        return [[1.0]]

    index = SemanticIndex(RECORDS, slow, min_similarity=0.1)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await index.retrieve("question")


def test_embeddings_are_returned_in_index_order() -> None:
    body = json.dumps(
        {
            "data": [
                {"index": 1, "embedding": [0, 1]},
                {"index": 0, "embedding": [1.5, 0]},
            ]
        }
    )

    assert parse_embeddings(body, 2) == [[1.5, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"embedding": [1.0]}]},
        {"data": [{"embedding": []}, {"embedding": []}]},
        {"data": [{"embedding": [1.0]}, {"embedding": [1.0, 2.0]}]},
        {"data": [{"embedding": ["x"]}, {"embedding": [1.0]}]},
        {"data": [{"embedding": [True]}, {"embedding": [1.0]}]},
        {"data": "nope"},
        [],
    ],
)
def test_unusable_embedding_bodies_are_invalid_content(payload) -> None:
    with pytest.raises(InvalidContent):
        parse_embeddings(json.dumps(payload), 2)


EMBED_PROFILE = ServiceProfile(
    base_url="http://embed.test/v1", model="embed-model", auth_mode="bearer", api_key="k"
)


def embed_provider(responder, *, with_profile: bool = True):
    recorder = RoleTransport("embed", responder)
    transports = {
        role: RoleTransport(role, unexpected(role)).transport
        for role in ("llm", "rag", "stt", "tts")
    }
    profiles = {"llm": LLM_PROFILE, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": TTS_PROFILE}
    if with_profile:
        profiles["embed"] = EMBED_PROFILE
        transports["embed"] = recorder.transport
    provider = OpenAICompatibleProvider(
        profiles, transports=transports, sleep=SleepRecorder(), clock=Clock()
    )
    return provider, recorder


async def test_embed_posts_the_model_and_texts_with_the_bearer_key() -> None:
    body = {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}]}
    provider, recorder = embed_provider(lambda request, attempt: json_response(body))

    vectors = await provider.embed(["one", "two"])

    request = recorder.requests[0]
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert str(request.url) == "http://embed.test/v1/embeddings"
    assert json.loads(request.content) == {"model": "embed-model", "input": ["one", "two"]}
    assert request.headers["authorization"] == "Bearer k"
    await provider.close()


async def test_embed_retries_a_server_error_once_then_succeeds() -> None:
    body = {"data": [{"index": 0, "embedding": [1, 0]}]}

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_response({}, 503) if attempt == 1 else json_response(body)

    provider, recorder = embed_provider(responder)

    assert await provider.embed(["one"]) == [[1.0, 0.0]]
    assert len(recorder.requests) == 2
    await provider.close()


async def test_embed_without_a_profile_is_a_configuration_error() -> None:
    provider, _ = embed_provider(unexpected("embed"), with_profile=False)

    with pytest.raises(AppError) as error:
        await provider.embed(["one"])

    assert error.value.code == "provider_configuration"
    await provider.close()


async def test_an_unusable_embedding_body_maps_to_invalid_provider_response() -> None:
    provider, _ = embed_provider(lambda request, attempt: json_response({"data": []}))

    with pytest.raises(AppError) as error:
        await provider.embed(["one"])

    assert error.value.code == "invalid_provider_response"
    await provider.close()
