"""Unit tests for language-correct speech profile selection (FEAT-02)."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from app.config import SpeechProfile
from app.errors import AppError
from app.providers import OpenAICompatibleProvider
from tests.unit.provider_support import (
    LLM_PROFILE,
    RAG_PROFILE,
    STT_PROFILE,
    TTS_PROFILE,
    Clock,
    RoleTransport,
    SleepRecorder,
    streamed_response,
)

ES_PROFILE = replace(
    TTS_PROFILE,
    base_url="http://tts-es.test/v1",
    model="tts-es-model",
    voice="es-voice",
    audio_format="wav",
    max_input_chars=200,
)


def wav_bytes() -> bytes:
    from tests.support.fakes import make_wav

    return make_wav()


def audio_responder(body: bytes, headers: dict[str, str] | None = None):
    resolved = headers or {"content-type": "audio/wav"}

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(body, 200, resolved)

    return responder


def unexpected(role):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise AssertionError(f"unexpected {role} call")

    return responder


def build(profiles, responders):
    roles = ("llm", "rag", "stt", "tts", "tts_es")
    recorders = {
        role: RoleTransport(role, responders.get(role, unexpected(role)))
        for role in roles
    }
    provider = OpenAICompatibleProvider(
        profiles,
        transports={role: recorders[role].transport for role in roles if role in profiles},
        capacity=4,
        sleep=SleepRecorder(),
        clock=Clock(),
    )
    return provider, recorders


def base_profiles():
    return {
        "llm": LLM_PROFILE,
        "rag": RAG_PROFILE,
        "stt": STT_PROFILE,
        "tts": TTS_PROFILE,
    }


async def test_spanish_synthesis_uses_the_spanish_profile_and_never_the_english_one():
    profiles = {**base_profiles(), "tts_es": ES_PROFILE}
    provider, recorders = build(
        profiles,
        {"tts_es": audio_responder(wav_bytes())},
    )

    audio = await provider.synthesize("Hola, ¿en qué puedo ayudarte?", "es")

    assert audio.media_type == "audio/wav"
    assert recorders["tts"].requests == []
    request = recorders["tts_es"].requests[0]
    assert str(request.url) == "http://tts-es.test/v1/audio/speech"
    payload = json.loads(request.content)
    assert payload["model"] == "tts-es-model"
    assert payload["voice"] == "es-voice"
    assert payload["response_format"] == "wav"
    assert payload["input"].startswith("Hola")


async def test_english_synthesis_uses_the_english_profile_even_when_spanish_exists():
    profiles = {**base_profiles(), "tts_es": ES_PROFILE}
    provider, recorders = build(
        profiles,
        {"tts": audio_responder(b"\xff\xfbmp3", {"content-type": "audio/mpeg"})},
    )

    await provider.synthesize("Hello there", "en")

    assert recorders["tts_es"].requests == []
    request = recorders["tts"].requests[0]
    assert str(request.url) == "http://tts.test/v1/audio/speech"
    assert json.loads(request.content)["model"] == "tts-model"


async def test_english_is_the_default_synthesis_language():
    provider, recorders = build(
        base_profiles(),
        {"tts": audio_responder(b"\xff\xfbmp3", {"content-type": "audio/mpeg"})},
    )

    await provider.synthesize("Hello there")

    assert len(recorders["tts"].requests) == 1


async def test_spanish_synthesis_without_a_spanish_profile_fails_with_provider_configuration():
    provider, recorders = build(
        base_profiles(),
        {"tts": audio_responder(b"\xff\xfbmp3", {"content-type": "audio/mpeg"})},
    )

    with pytest.raises(AppError) as excinfo:
        await provider.synthesize("Hola", "es")

    assert excinfo.value.code == "provider_configuration"
    assert excinfo.value.status == 503
    assert recorders["tts"].requests == []


async def test_spanish_profile_chunks_at_its_own_cap():
    profiles = {**base_profiles(), "tts_es": ES_PROFILE}
    provider, recorders = build(
        profiles,
        {"tts_es": audio_responder(wav_bytes())},
    )

    await provider.synthesize("a" * 401, "es")

    assert len(recorders["tts_es"].requests) == 3
    payloads = [json.loads(request.content) for request in recorders["tts_es"].requests]
    assert [len(payload["input"]) for payload in payloads] == [200, 200, 1]
