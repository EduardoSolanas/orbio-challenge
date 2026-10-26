"""Unit tests for the OpenAI-compatible provider adapter (PC sections 1-3, IC 4/7/11/12).

The real httpx client stack runs against per-role ``httpx.MockTransport``
handlers: no SDK, no real host, no credential. Sleep and clock are injected so
retry jitter and deadline arithmetic are deterministic: transcription."""

from __future__ import annotations

import json

import pytest

from gateway.errors import AppError
from tests.unit.gateway.providers.provider_support import (
    SleepRecorder,
    build_provider,
    json_response,
    single_response,
)


def transcript_envelope(target_bytes: int | None = None) -> str:
    payload: dict[str, object] = {"text": "My order arrived broken."}
    raw = json.dumps(payload)
    if target_bytes is None:
        return raw
    payload["pad"] = ""
    pad = target_bytes - len(json.dumps(payload).encode("utf-8"))
    assert pad >= 0
    payload["pad"] = "x" * pad
    raw = json.dumps(payload)
    assert len(raw.encode("utf-8")) == target_bytes
    return raw


async def test_transcribe_sends_the_exact_multipart_request():
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "hello"})}
    )

    await provider.transcribe(b"RIFF-validated-wav-bytes")

    request = recorders["stt"].requests[0]
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="file"; filename="upload.wav"' in body
    assert b"Content-Type: audio/wav" in body
    assert b"RIFF-validated-wav-bytes" in body
    assert b'name="model"' in body and b"stt-model" in body
    assert b'name="response_format"' in body and b"json" in body


async def test_transcribe_trims_text_and_ignores_extra_response_fields():
    provider, _ = build_provider(
        {
            "stt": lambda request, attempt: json_response(
                {"text": "  hello there  ", "language": "en"}
            )
        }
    )

    assert await provider.transcribe(b"wav") == "hello there"


@pytest.mark.parametrize(
    "payload", [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}]
)
async def test_empty_or_nonstring_transcript_is_not_retried(payload: dict):
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response(payload)}
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "empty_transcription"
    assert caught.value.status == 422
    assert caught.value.retryable is False
    assert len(recorders["stt"].requests) == 1


@pytest.mark.parametrize("length,code", [(4000, None), (4001, "transcript_too_long")])
async def test_transcript_length_boundary(length: int, code: str | None):
    provider, recorders = build_provider(
        {"stt": lambda request, attempt: json_response({"text": "a" * length})}
    )

    if code is None:
        assert await provider.transcribe(b"wav") == "a" * length
        assert len(recorders["stt"].requests) == 1
    else:
        with pytest.raises(AppError) as caught:
            await provider.transcribe(b"wav")
        assert caught.value.code == code
        assert caught.value.status == 422
        assert len(recorders["stt"].requests) == 1


@pytest.mark.parametrize("size", [1024 * 1024, 1024 * 1024 + 1])
async def test_transcript_success_json_one_mib_boundary(size: int):
    provider, recorders = build_provider(
        {"stt": single_response(transcript_envelope(size))}
    )

    if size == 1024 * 1024:
        assert (await provider.transcribe(b"wav")).startswith("My order")
        assert len(recorders["stt"].requests) == 1
    else:
        with pytest.raises(AppError) as caught:
            await provider.transcribe(b"wav")
        assert caught.value.code == "invalid_provider_response"
        assert len(recorders["stt"].requests) == 2


async def test_malformed_transcript_json_retries_once_then_502():
    provider, recorders = build_provider({"stt": single_response("not json")})

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["stt"].requests) == 2


async def test_gzip_transcript_success_is_rejected_without_decompression():
    import gzip

    compressed = gzip.compress(json.dumps({"text": "hello"}).encode("utf-8"))
    provider, recorders = build_provider(
        {"stt": single_response(compressed, headers={"content-encoding": "gzip"})}
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["stt"].requests) == 2


async def test_stt_429_retries_once_then_provider_unavailable():
    sleeps = SleepRecorder()
    provider, recorders = build_provider(
        {"stt": single_response("{}", status=429)}, sleep=sleeps
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["stt"].requests) == 2
    assert len(sleeps.calls) == 1
    assert 0.25 <= sleeps.calls[0] <= 0.5


async def test_stt_401_is_configuration_error_with_one_attempt_and_bounded_body():
    provider, recorders = build_provider(
        {"stt": single_response(b"e" * (64 * 1024 + 1), status=401)}
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(b"wav")

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert len(recorders["stt"].requests) == 1
