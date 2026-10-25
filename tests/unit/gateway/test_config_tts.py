"""Unit tests for TTS format and per-request input-cap settings (PC §3)."""

from __future__ import annotations

import pytest

from backend.gateway.config import (
    DEFAULT_TTS_AUDIO_FORMAT,
    DEFAULT_TTS_MAX_INPUT_CHARS,
    SpeechProfile,
    Settings,
)
from backend.gateway.errors import CONFIGURATION_INVALID_MESSAGE

SESSION_DIR = "/tmp/orbio-sessions"


def env(**overrides):
    values = {
        "AI_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": "http://llm.test/v1",
        "LLM_MODEL": "llm-model",
        "LLM_AUTH_MODE": "none",
        "LLM_API_KEY": "",
        "STT_BASE_URL": "http://stt.test/v1",
        "STT_MODEL": "stt-model",
        "STT_AUTH_MODE": "none",
        "STT_API_KEY": "",
        "TTS_BASE_URL": "http://tts.test/v1",
        "TTS_MODEL": "tts-model",
        "TTS_AUTH_MODE": "none",
        "TTS_API_KEY": "",
        "TTS_VOICE": "troy",
        "SESSION_DIR": SESSION_DIR,
        "PROVIDER_MAX_CONCURRENCY": "4",
        "BACKEND_API_BASE_URL": "http://backend-api.test",
    }
    values.update(overrides)
    return values


def test_tts_format_and_cap_default_to_mp3_and_four_thousand():
    settings = Settings.from_env(env())

    assert isinstance(settings.tts, SpeechProfile)
    assert settings.tts.audio_format == DEFAULT_TTS_AUDIO_FORMAT == "mp3"
    assert settings.tts.max_input_chars == DEFAULT_TTS_MAX_INPUT_CHARS == 4000


def test_explicit_groq_orpheus_wav_profile_is_resolved():
    settings = Settings.from_env(env(TTS_AUDIO_FORMAT="wav", TTS_MAX_INPUT_CHARS="200"))

    assert settings.tts.audio_format == "wav"
    assert settings.tts.max_input_chars == 200


def test_mp3_with_the_full_four_thousand_cap_is_accepted():
    settings = Settings.from_env(env(TTS_AUDIO_FORMAT="mp3", TTS_MAX_INPUT_CHARS="4000"))

    assert settings.tts.audio_format == "mp3"
    assert settings.tts.max_input_chars == 4000


@pytest.mark.parametrize("value", ["WAV", "Wav", "ogg", "mpeg", " wav", "wav "])
def test_unknown_or_case_mismatched_audio_format_fails(value):
    with pytest.raises(RuntimeError) as caught:
        Settings.from_env(env(TTS_AUDIO_FORMAT=value))

    assert str(caught.value) == CONFIGURATION_INVALID_MESSAGE


@pytest.mark.parametrize("value", ["1", "199", "200", "4000"])
def test_in_range_max_input_chars_is_accepted(value):
    settings = Settings.from_env(env(TTS_AUDIO_FORMAT="wav", TTS_MAX_INPUT_CHARS=value))

    assert settings.tts.max_input_chars == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "4001", "9999", "abc", "2.5", "1e3"])
def test_malformed_or_out_of_range_max_input_chars_fails(value):
    with pytest.raises(RuntimeError) as caught:
        Settings.from_env(env(TTS_AUDIO_FORMAT="wav", TTS_MAX_INPUT_CHARS=value))

    assert str(caught.value) == CONFIGURATION_INVALID_MESSAGE


def test_mp3_with_a_smaller_cap_is_rejected_before_startup():
    with pytest.raises(RuntimeError) as caught:
        Settings.from_env(env(TTS_AUDIO_FORMAT="mp3", TTS_MAX_INPUT_CHARS="200"))

    assert str(caught.value) == CONFIGURATION_INVALID_MESSAGE


def test_mp3_default_cap_stays_four_thousand_when_format_is_explicit():
    settings = Settings.from_env(env(TTS_AUDIO_FORMAT="mp3"))

    assert settings.tts.max_input_chars == 4000
