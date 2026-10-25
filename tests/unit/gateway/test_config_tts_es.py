"""Unit tests for the optional Spanish TTS profile settings."""

from __future__ import annotations

import pytest

from backend.gateway.config import SpeechProfile, Settings
from backend.gateway.errors import CONFIGURATION_INVALID_MESSAGE

SESSION_DIR = "/tmp/orbio-sessions"

BASE_ENV = {
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

COMPLETE_ES = {
    "TTS_ES_BASE_URL": "http://tts-es.test/v1",
    "TTS_ES_MODEL": "tts-es-model",
    "TTS_ES_AUTH_MODE": "none",
    "TTS_ES_API_KEY": "",
    "TTS_ES_VOICE": "es-voice",
    "TTS_ES_AUDIO_FORMAT": "wav",
    "TTS_ES_MAX_INPUT_CHARS": "200",
}


def env(**overrides):
    return {**BASE_ENV, **overrides}


def test_all_blank_spanish_tts_fields_leave_the_profile_absent():
    settings = Settings.from_env(env())

    assert settings.tts_es is None
    assert isinstance(settings.tts, SpeechProfile)


def test_a_complete_spanish_profile_is_resolved_independently_of_english():
    settings = Settings.from_env(env(**COMPLETE_ES))

    assert isinstance(settings.tts_es, SpeechProfile)
    assert settings.tts_es.base_url == "http://tts-es.test/v1"
    assert settings.tts_es.model == "tts-es-model"
    assert settings.tts_es.voice == "es-voice"
    assert settings.tts_es.audio_format == "wav"
    assert settings.tts_es.max_input_chars == 200
    assert settings.tts.model == "tts-model"
    assert settings.tts.voice == "troy"


@pytest.mark.parametrize("field", sorted(COMPLETE_ES))
def test_any_supplied_spanish_value_requires_the_whole_profile(field):
    supplied = COMPLETE_ES[field]
    if field == "TTS_ES_API_KEY":
        # A non-empty key with auth_mode none is itself invalid; either way a
        # partial Spanish profile must never be silently accepted.
        supplied = "spanish-key"
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(env(**{field: supplied}))

    assert str(excinfo.value) == CONFIGURATION_INVALID_MESSAGE


def test_spanish_profile_requires_an_explicit_audio_format():
    partial = {key: value for key, value in COMPLETE_ES.items() if key != "TTS_ES_AUDIO_FORMAT"}
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**partial))


def test_spanish_profile_requires_an_explicit_max_input_chars():
    partial = {
        key: value
        for key, value in COMPLETE_ES.items()
        if key != "TTS_ES_MAX_INPUT_CHARS"
    }
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**partial))


def test_spanish_bearer_mode_requires_its_own_key_and_never_inherits_the_english_key():
    with pytest.raises(RuntimeError):
        Settings.from_env(
            env(**{**COMPLETE_ES, "TTS_ES_AUTH_MODE": "bearer", "TTS_API_KEY": "english-key"})
        )

    settings = Settings.from_env(
        env(
            **{
                **COMPLETE_ES,
                "TTS_ES_AUTH_MODE": "bearer",
                "TTS_ES_API_KEY": "spanish-key",
                "TTS_API_KEY": "english-key",
                "TTS_AUTH_MODE": "bearer",
            }
        )
    )
    assert settings.tts_es.api_key == "spanish-key"
    assert settings.tts.api_key == "english-key"


def test_spanish_key_without_bearer_mode_is_rejected():
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**{**COMPLETE_ES, "TTS_ES_API_KEY": "unused-key"}))


def test_spanish_mp3_profile_must_keep_the_full_cap():
    with pytest.raises(RuntimeError):
        Settings.from_env(
            env(**{**COMPLETE_ES, "TTS_ES_AUDIO_FORMAT": "mp3", "TTS_ES_MAX_INPUT_CHARS": "200"})
        )

    settings = Settings.from_env(
        env(
            **{
                **COMPLETE_ES,
                "TTS_ES_AUDIO_FORMAT": "mp3",
                "TTS_ES_MAX_INPUT_CHARS": "4000",
            }
        )
    )
    assert settings.tts_es.audio_format == "mp3"
    assert settings.tts_es.max_input_chars == 4000


def test_spanish_invalid_format_or_cap_is_rejected():
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**{**COMPLETE_ES, "TTS_ES_AUDIO_FORMAT": "ogg"}))
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**{**COMPLETE_ES, "TTS_ES_MAX_INPUT_CHARS": "0"}))
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**{**COMPLETE_ES, "TTS_ES_MAX_INPUT_CHARS": "many"}))
    with pytest.raises(RuntimeError):
        Settings.from_env(env(**{**COMPLETE_ES, "TTS_ES_BASE_URL": "tts-es.test/v1"}))
