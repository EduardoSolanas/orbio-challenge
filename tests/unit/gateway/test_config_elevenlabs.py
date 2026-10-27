"""The ElevenLabs auth mode is for speech profiles only, with a key and mp3 replies."""

from __future__ import annotations

import pytest

from gateway.config import ELEVENLABS, Settings
from gateway.errors import CONFIGURATION_INVALID_MESSAGE
from tests.unit.gateway.test_config_tts_es import BASE_ENV, COMPLETE_ES

ELEVEN_ES = {
    **COMPLETE_ES,
    "TTS_ES_BASE_URL": "https://api.elevenlabs.io/v1",
    "TTS_ES_MODEL": "eleven_multilingual_v2",
    "TTS_ES_AUTH_MODE": ELEVENLABS,
    "TTS_ES_API_KEY": "xi-secret",
    "TTS_ES_AUDIO_FORMAT": "mp3",
    "TTS_ES_MAX_INPUT_CHARS": "4000",
}


def test_a_complete_elevenlabs_spanish_profile_is_accepted() -> None:
    settings = Settings.from_env({**BASE_ENV, **ELEVEN_ES})

    assert settings.tts_es.auth_mode == ELEVENLABS
    assert settings.tts_es.api_key == "xi-secret"
    assert settings.tts.auth_mode == "none"


@pytest.mark.parametrize(
    "overrides",
    [
        {"TTS_ES_API_KEY": ""},
        {"TTS_ES_AUDIO_FORMAT": "wav", "TTS_ES_MAX_INPUT_CHARS": "200"},
    ],
    ids=["no_key", "wav"],
)
def test_an_elevenlabs_profile_needs_a_key_and_mp3(overrides) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env({**BASE_ENV, **ELEVEN_ES, **overrides})

    assert str(excinfo.value) == CONFIGURATION_INVALID_MESSAGE


@pytest.mark.parametrize("role", ["LLM", "STT"])
def test_elevenlabs_is_refused_for_roles_that_do_not_speak(role) -> None:
    env = {**BASE_ENV, f"{role}_AUTH_MODE": ELEVENLABS, f"{role}_API_KEY": "xi-secret"}

    with pytest.raises(RuntimeError):
        Settings.from_env(env)


def test_elevenlabs_works_for_the_main_speech_role_too() -> None:
    env = {
        **BASE_ENV,
        "TTS_AUTH_MODE": ELEVENLABS,
        "TTS_API_KEY": "xi-secret",
        "TTS_AUDIO_FORMAT": "mp3",
        "TTS_MAX_INPUT_CHARS": "4000",
    }

    assert Settings.from_env(env).tts.auth_mode == ELEVENLABS
