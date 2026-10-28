"""ElevenLabs speech profiles: its own route, header and body, mp3 only."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gateway.config import ELEVENLABS
from gateway.providers.client import SynthesizedAudio
from tests.unit.gateway.providers.provider_support import (
    LLM_PROFILE,
    RAG_PROFILE,
    STT_PROFILE,
    TTS_PROFILE,
    build_provider,
    json_response,
)
from tests.unit.gateway.providers.test_speech_synthesis import audio_responder

ELEVENLABS_PROFILE = replace(
    TTS_PROFILE,
    base_url="https://api.elevenlabs.io/v1",
    model="eleven_multilingual_v2",
    auth_mode=ELEVENLABS,
    api_key="xi-secret",
    voice="voice/id 1",
    audio_format="mp3",
    max_input_chars=4000,
)


def provider_with_elevenlabs(responder):
    profiles = {
        "llm": LLM_PROFILE,
        "rag": RAG_PROFILE,
        "stt": STT_PROFILE,
        "tts": ELEVENLABS_PROFILE,
    }
    return build_provider({"tts": responder}, profiles=profiles)


async def test_speech_goes_to_the_elevenlabs_route_with_its_header_and_body() -> None:
    provider, recorders = provider_with_elevenlabs(
        audio_responder(b"ID3-mp3", {"content-type": "audio/mpeg"})
    )

    result = await provider.synthesize("Hola, todo guardado.")

    assert result == SynthesizedAudio(data=b"ID3-mp3", media_type="audio/mpeg")
    request = recorders["tts"].requests[0]
    assert str(request.url) == (
        "https://api.elevenlabs.io/v1/text-to-speech/voice%2Fid%201"
        "?output_format=mp3_44100_128"
    )
    assert request.headers["xi-api-key"] == "xi-secret"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "text": "Hola, todo guardado.",
        "model_id": "eleven_multilingual_v2",
    }


@pytest.mark.parametrize("auth_mode", ["none", "bearer"])
async def test_other_auth_modes_keep_the_openai_speech_route(auth_mode) -> None:
    profile = replace(
        ELEVENLABS_PROFILE,
        auth_mode=auth_mode,
        api_key="token" if auth_mode == "bearer" else "",
    )
    profiles = {
        "llm": LLM_PROFILE,
        "rag": RAG_PROFILE,
        "stt": STT_PROFILE,
        "tts": profile,
    }
    provider, recorders = build_provider(
        {"tts": audio_responder(b"ID3", {"content-type": "audio/mpeg"})},
        profiles=profiles,
    )

    await provider.synthesize("Hello")

    request = recorders["tts"].requests[0]
    assert request.url.path == "/v1/audio/speech"
    assert "xi-api-key" not in request.headers
    assert json.loads(request.content)["input"] == "Hello"


async def test_transcription_goes_to_the_elevenlabs_route_with_its_header_and_fields() -> (
    None
):
    stt = replace(
        STT_PROFILE,
        base_url="https://api.elevenlabs.io/v1",
        model="scribe_v1",
        auth_mode=ELEVENLABS,
        api_key="xi-secret",
    )
    profiles = {"llm": LLM_PROFILE, "rag": RAG_PROFILE, "stt": stt, "tts": TTS_PROFILE}
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "Hola, ¿qué tal?"})},
        profiles=profiles,
    )

    assert await provider.transcribe(b"RIFFdata") == "Hola, ¿qué tal?"

    request = recorders["stt"].requests[0]
    assert str(request.url) == "https://api.elevenlabs.io/v1/speech-to-text"
    assert request.headers["xi-api-key"] == "xi-secret"
    assert "authorization" not in request.headers
    body = request.content
    assert b'name="model_id"' in body and b"scribe_v1" in body
    assert b'name="tag_audio_events"' in body and b"false" in body
    assert b'name="model"' not in body and b"response_format" not in body
