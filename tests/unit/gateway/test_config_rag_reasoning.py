"""The RAG role's reasoning effort: inherited from the chat model, set on its own, or left empty."""

from __future__ import annotations

from gateway.config import Settings
from tests.unit.gateway.test_config_tts_es import BASE_ENV

OWN_RAG = {
    "RAG_BASE_URL": "http://rag.test/v1",
    "RAG_MODEL": "rag-model",
    "RAG_AUTH_MODE": "none",
    "RAG_OUTPUT_MODE": "json_object",
}


def settings(**overrides) -> Settings:
    return Settings.from_env({**BASE_ENV, **overrides})


def test_the_rag_role_inherits_the_chat_models_effort_when_it_is_not_configured() -> (
    None
):
    result = settings(LLM_REASONING_EFFORT="low")

    assert result.rag.reasoning_effort == "low"
    assert result.rag.model == result.llm.model


def test_an_effort_on_its_own_overrides_it_while_everything_else_is_inherited() -> None:
    result = settings(LLM_REASONING_EFFORT="low", RAG_REASONING_EFFORT="high")

    assert result.rag.reasoning_effort == "high"
    assert (result.rag.base_url, result.rag.model) == (
        result.llm.base_url,
        result.llm.model,
    )
    assert result.llm.reasoning_effort == "low"


def test_an_own_rag_role_takes_its_effort_from_its_own_setting() -> None:
    result = settings(
        LLM_REASONING_EFFORT="low", RAG_REASONING_EFFORT="medium", **OWN_RAG
    )

    assert result.rag.model == "rag-model"
    assert result.rag.reasoning_effort == "medium"


def test_an_own_rag_role_without_an_effort_does_not_borrow_the_chat_models() -> None:
    result = settings(LLM_REASONING_EFFORT="low", **OWN_RAG)

    assert result.rag.reasoning_effort is None
