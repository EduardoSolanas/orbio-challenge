"""BACKEND_API_TIMEOUT_SECONDS and the optional Spanish speech profile's remaining edges."""

from __future__ import annotations

import pytest

from gateway.config import (
    DEFAULT_BACKEND_API_TIMEOUT_SECONDS,
    MAX_BACKEND_API_TIMEOUT_SECONDS,
    MIN_TTS_MAX_INPUT_CHARS,
    Settings,
)
from gateway.errors import CONFIGURATION_INVALID_MESSAGE
from tests.unit.gateway.test_config_tts_es import BASE_ENV, COMPLETE_ES


def settings(**overrides) -> Settings:
    return Settings.from_env({**BASE_ENV, **overrides})


def test_the_timeout_defaults_when_it_is_not_set() -> None:
    assert settings().backend_api_timeout_seconds == DEFAULT_BACKEND_API_TIMEOUT_SECONDS
    assert (
        settings(BACKEND_API_TIMEOUT_SECONDS="  ").backend_api_timeout_seconds
        == DEFAULT_BACKEND_API_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    "value", ["2.5", "0.1", str(MAX_BACKEND_API_TIMEOUT_SECONDS), " 3 "]
)
def test_a_positive_timeout_up_to_the_maximum_is_used(value) -> None:
    assert settings(
        BACKEND_API_TIMEOUT_SECONDS=value
    ).backend_api_timeout_seconds == float(value)


@pytest.mark.parametrize("value", ["0", "0.0", "-1", "60.5", "soon", "nan"])
def test_a_zero_negative_too_large_or_non_numeric_timeout_is_rejected(value) -> None:
    with pytest.raises(RuntimeError, match=CONFIGURATION_INVALID_MESSAGE):
        settings(BACKEND_API_TIMEOUT_SECONDS=value)


def test_the_spanish_speech_profile_keeps_its_own_auth_mode_and_accepts_the_smallest_limit() -> (
    None
):
    result = settings(
        **{**COMPLETE_ES, "TTS_ES_MAX_INPUT_CHARS": str(MIN_TTS_MAX_INPUT_CHARS)}
    )

    assert result.tts_es.auth_mode == "none"
    assert result.tts_es.max_input_chars == MIN_TTS_MAX_INPUT_CHARS


def test_the_spanish_speech_profile_rejects_a_limit_below_the_smallest() -> None:
    with pytest.raises(RuntimeError, match=CONFIGURATION_INVALID_MESSAGE):
        settings(
            **{
                **COMPLETE_ES,
                "TTS_ES_MAX_INPUT_CHARS": str(MIN_TTS_MAX_INPUT_CHARS - 1),
            }
        )
