"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic: speech."""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.errors import AppError
from tests.unit.gateway.providers.provider_support import (
    ChunkedStream,
    SleepRecorder,
    audio_responder,
    build_provider,
)


async def test_synthesize_sends_the_exact_speech_request():
    provider, recorders = build_provider({"tts": audio_responder(b"ID3-mp3")})

    result = await provider.synthesize("Saved locally as x.")

    assert result.data == b"ID3-mp3"
    assert result.media_type == "audio/mpeg"

    request = recorders["tts"].requests[0]
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "model": "tts-model",
        "voice": "alloy",
        "input": "Saved locally as x.",
        "response_format": "mp3",
    }


@pytest.mark.parametrize(
    "content_type",
    ["audio/mpeg", "audio/mp3", "Audio/MPEG", "audio/mpeg; charset=binary"],
)
async def test_accepted_mp3_content_types(content_type: str):
    provider, _ = build_provider(
        {"tts": audio_responder(b"mp3", {"content-type": content_type})}
    )

    assert (await provider.synthesize("hello")).data == b"mp3"


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/json", "audio/wav", "", None]
)
async def test_non_mp3_success_content_is_one_attempt_invalid_response(content_type):
    headers = {} if content_type is None else {"content-type": content_type}
    provider, recorders = build_provider(
        {"tts": audio_responder(b"wav-bytes", headers)}
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert caught.value.status == 502
    assert len(recorders["tts"].requests) == 1


async def test_empty_mp3_body_is_invalid_response_with_one_attempt():
    provider, recorders = build_provider({"tts": audio_responder(b"")})

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


@pytest.mark.parametrize("size", [5 * 1024 * 1024, 5 * 1024 * 1024 + 1])
async def test_mp3_five_mib_boundary(size: int):
    provider, recorders = build_provider({"tts": audio_responder(b"m" * size)})

    if size == 5 * 1024 * 1024:
        assert len((await provider.synthesize("hello")).data) == size
        assert len(recorders["tts"].requests) == 1
    else:
        with pytest.raises(AppError) as caught:
            await provider.synthesize("hello")
        assert caught.value.code == "invalid_provider_response"
        assert len(recorders["tts"].requests) == 1


async def test_gzip_mp3_success_is_rejected_without_decompression():
    import gzip

    provider, recorders = build_provider(
        {
            "tts": audio_responder(
                gzip.compress(b"mp3-bytes"),
                {"content-type": "audio/mpeg", "content-encoding": "gzip"},
            )
        }
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


async def test_mp3_streamed_chunks_are_joined_after_a_bounded_read():
    stream = ChunkedStream([b"ID3", b"-part2", b"-part3"])

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200, stream=stream, headers={"content-type": "audio/mpeg"}
        )

    provider, _ = build_provider({"tts": responder})

    assert (await provider.synthesize("hello")).data == b"ID3-part2-part3"
    assert stream.closed is True


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_tts_transient_failure_is_single_attempt_provider_unavailable(
    status: int,
):
    sleeps = SleepRecorder()
    provider, recorders = build_provider(
        {
            "tts": audio_responder(
                b"error", {"content-type": "text/plain"}, status=status
            )
        },
        sleep=sleeps,
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_unavailable"
    assert caught.value.status == 503
    assert len(recorders["tts"].requests) == 1
    assert sleeps.calls == []


@pytest.mark.parametrize("status", [302, 401, 404, 415])
async def test_tts_configuration_status_is_single_attempt_nonretryable(status: int):
    provider, recorders = build_provider(
        {"tts": audio_responder(b"", {"content-type": "text/plain"}, status=status)}
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert len(recorders["tts"].requests) == 1


async def test_tts_connection_failure_is_single_attempt_provider_unavailable():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, recorders = build_provider({"tts": responder})

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["tts"].requests) == 1
