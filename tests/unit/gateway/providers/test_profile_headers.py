"""A profile's extra headers go out on every request next to the auth header."""

from __future__ import annotations

from dataclasses import replace

from tests.unit.gateway.providers.provider_support import (
    LLM_PROFILE,
    RAG_PROFILE,
    STT_PROFILE,
    TTS_PROFILE,
    build_provider,
)
from tests.unit.gateway.providers.test_speech_synthesis import audio_responder


async def test_extra_headers_are_sent_with_the_bearer_token() -> None:
    tts = replace(
        TTS_PROFILE,
        auth_mode="bearer",
        api_key="secret",
        headers=(("x-opencode-session", "northstar"),),
        audio_format="mp3",
        max_input_chars=4000,
    )
    profiles = {"llm": LLM_PROFILE, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": tts}
    provider, recorders = build_provider(
        {"tts": audio_responder(b"ID3", {"content-type": "audio/mpeg"})},
        profiles=profiles,
    )

    await provider.synthesize("Hello")

    request = recorders["tts"].requests[0]
    assert request.headers["x-opencode-session"] == "northstar"
    assert request.headers["authorization"] == "Bearer secret"
