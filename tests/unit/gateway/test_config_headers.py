"""Extra static request headers on a provider profile: validated, inherited, sent."""

from __future__ import annotations

import json

import pytest

from gateway.config import Settings
from gateway.errors import CONFIGURATION_INVALID_MESSAGE
from tests.unit.gateway.test_config_tts_es import BASE_ENV

SESSION = {"x-opencode-session": "northstar"}


def env(**overrides):
    return {**BASE_ENV, **overrides}


def test_a_json_object_becomes_the_profiles_headers() -> None:
    settings = Settings.from_env(env(LLM_HEADERS=json.dumps(SESSION)))

    assert settings.llm.headers == (("x-opencode-session", "northstar"),)
    assert settings.stt.headers == ()


def test_the_default_is_no_extra_headers() -> None:
    assert Settings.from_env(env()).llm.headers == ()


def test_the_answer_model_inherits_the_llm_headers() -> None:
    settings = Settings.from_env(env(LLM_HEADERS=json.dumps(SESSION)))

    assert settings.rag.headers == settings.llm.headers


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "{}",
        '"text"',
        '{"x": 1}',
        '{"bad name": "v"}',
        '{"x": "line\nbreak"}',
        '{"Authorization": "Bearer other"}',
        '{"xi-api-key": "k"}',
        '{"Content-Type": "text/plain"}',
        json.dumps({f"x-{index}": "v" for index in range(9)}),
        json.dumps({"x": "v" * 257}),
    ],
)
def test_a_malformed_or_reserved_header_setting_is_refused(raw) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(env(LLM_HEADERS=raw))

    assert str(excinfo.value) == CONFIGURATION_INVALID_MESSAGE


def test_a_deeply_nested_header_setting_is_refused_not_raised() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(env(LLM_HEADERS="[" * 100000))

    assert str(excinfo.value) == CONFIGURATION_INVALID_MESSAGE
