"""Unit tests for the optional embedding service settings."""

from __future__ import annotations

import pytest

from app.config import DEFAULT_EMBED_MIN_SIMILARITY, ServiceProfile, Settings
from app.errors import CONFIGURATION_INVALID_MESSAGE
from tests.unit.test_config_tts_es import BASE_ENV

COMPLETE = {
    "EMBED_BASE_URL": "http://embed.test/v1/",
    "EMBED_MODEL": "embed-model",
    "EMBED_AUTH_MODE": "none",
}


def env(**overrides):
    return {**BASE_ENV, **overrides}


def test_blank_embedding_settings_keep_keyword_retrieval():
    settings = Settings.from_env(env())

    assert settings.embed is None
    assert settings.embed_min_similarity == DEFAULT_EMBED_MIN_SIMILARITY


def test_a_complete_profile_is_resolved_and_the_url_is_normalised():
    settings = Settings.from_env(env(**COMPLETE))

    assert settings.embed == ServiceProfile(
        base_url="http://embed.test/v1", model="embed-model", auth_mode="none", api_key=""
    )


def test_a_bearer_profile_carries_its_key():
    settings = Settings.from_env(
        env(**{**COMPLETE, "EMBED_AUTH_MODE": "bearer", "EMBED_API_KEY": "secret"})
    )

    assert settings.embed.auth_mode == "bearer"
    assert settings.embed.api_key == "secret"


@pytest.mark.parametrize("missing", ["EMBED_BASE_URL", "EMBED_MODEL", "EMBED_AUTH_MODE"])
def test_a_partial_profile_is_rejected(missing):
    values = {key: value for key, value in COMPLETE.items() if key != missing}

    with pytest.raises(RuntimeError, match=CONFIGURATION_INVALID_MESSAGE):
        Settings.from_env(env(**values))


@pytest.mark.parametrize(
    "extra",
    [
        {"EMBED_AUTH_MODE": "bearer"},
        {"EMBED_API_KEY": "secret"},
        {"EMBED_BASE_URL": "ftp://embed.test"},
    ],
)
def test_an_inconsistent_profile_is_rejected(extra):
    with pytest.raises(RuntimeError, match=CONFIGURATION_INVALID_MESSAGE):
        Settings.from_env(env(**{**COMPLETE, **extra}))


def test_the_similarity_floor_can_be_set():
    assert Settings.from_env(env(EMBED_MIN_SIMILARITY="0.6")).embed_min_similarity == 0.6


@pytest.mark.parametrize("raw", ["0", "1", "-0.2", "high", "1.5"])
def test_an_out_of_range_similarity_floor_is_rejected(raw):
    with pytest.raises(RuntimeError, match=CONFIGURATION_INVALID_MESSAGE):
        Settings.from_env(env(EMBED_MIN_SIMILARITY=raw))
