"""Transcription route: media guards, upload limits and STT error mapping.

Every case runs the real app factory, lifespan, `/api/transcriptions` route, the
real pure-ASGI upload guard and the real `OpenAICompatibleProvider` adapter over
per-role `httpx.MockTransport` handlers (IC §13.1: wire-failure cases inject the
actual HTTP transports). Only the external service is doubled; multipart parsing,
WAV validation, transcript trimming and error mapping are the shipped code.

The route never touches a Session: each case creates a real session first and
compares the session directory and file bytes before and after the upload.

Fixtures come from `tests/fixtures/audio/gen_wav.py` (stdlib `wave`/`struct`);
its docstring records every rate, duration and byte size.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.fixtures.audio import gen_wav
from tests.integration.support.api import raw_asgi
from tests.integration.support.intake import start_session
from tests.integration.support.transcription import (
    RecordingStream,
    assert_error,
    audio_client,
    json_wire,
    snapshot,
)

STT_PATH = "/v1/audio/transcriptions"
TRANSCRIPT = "My order NS-000123 arrived broken and this is urgent."


def stt_json(text: str):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire({"text": text})

    return responder


def stt_status(status: int):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire({"error": "unavailable"}, status=status)

    return responder


def stt_timeout():
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise httpx.TimeoutException("held read timeout", request=request)

    return responder


async def upload(client, payload: bytes, *, filename: str = "recording.wav"):
    return await client.post(
        "/api/transcriptions",
        files={"file": (filename, payload, "audio/wav")},
    )


# valid finite16 khz mono16 bit pcm wav -------------------------------------------------------------------


async def case_valid_finite16_khz_mono16_bit_pcm_wav(settings, session_dir):
    async with audio_client(settings, {"stt": stt_json(TRANSCRIPT)}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        payload = gen_wav.pcm_wav()
        assert len(payload) == 32044

        response = await upload(client, payload)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"text": TRANSCRIPT}
        assert len(rec["stt"].requests) == 1
        request = rec["stt"].requests[0]
        assert request.url.path == STT_PATH
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert request.headers["accept-encoding"] == "identity"
        body = request.content
        assert b'name="file"' in body
        assert b'filename="upload.wav"' in body
        assert payload in body
        assert snapshot(session_dir, session_id) == before
        assert rec["llm"].requests == [] and rec["tts"].requests == []


# all zero valid wav..valid pcm duration61 seconds: guards reject before any paid call -----------------------


async def case_all_zero_valid_wav(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.silence_wav(), status=422, code="silent_audio")


async def case_malformed_riff_header_bytes(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.malformed_wav(), status=422, code="invalid_audio")


async def case_declared_frames_exceed_actual_bytes(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.truncated_wav(), status=422, code="invalid_audio")


async def case_compressed_wav(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.compressed_wav(), status=415, code="unsupported_audio")


async def case_webm_payload_file(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.webm_bytes(), status=415, code="unsupported_audio")


async def case_valid_pcm_duration61_seconds(settings, session_dir):
    await assert_guard(settings, session_dir, gen_wav.long_wav(), status=422, code="invalid_audio")


# file10mib 1 bytes..chunked body exceeding multipart cap without content length: upload caps ---------------------------------------------


async def case_file10mib_1_bytes(settings, session_dir):
    async with audio_client(settings, {}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        payload = gen_wav.oversize_file()
        assert len(payload) == gen_wav.MAX_FILE_BYTES + 1
        response = await upload(client, payload)
        assert_error(response, status=413, code="input_too_large")
        assert rec["stt"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_multipart_total10mib_64kib_1(settings, session_dir):
    body, boundary = gen_wav.oversized_multipart()
    assert len(body) == gen_wav.MAX_MULTIPART_BYTES + 1
    async with audio_client(settings, {}) as (client, rec, app):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        status, headers, raw = await raw_asgi(
            app,
            method="POST",
            path="/api/transcriptions",
            chunks=[body[index : index + 64 * 1024] for index in range(0, len(body), 64 * 1024)],
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
                "content-length": str(len(body)),
            },
        )
        assert status == 413
        assert json.loads(raw)["error"]["code"] == "input_too_large"
        assert rec["stt"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_chunked_body_exceeding_multipart_cap_without_content_length(settings, session_dir):
    async with audio_client(settings, {}) as (client, rec, app):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        status, headers, raw = await raw_asgi(
            app,
            method="POST",
            path="/api/transcriptions",
            chunks=gen_wav.chunked_oversize(),
            headers={"content-type": "multipart/form-data; boundary=----orbio10chunked"},
        )
        assert status == 413
        assert json.loads(raw)["error"]["code"] == "input_too_large"
        assert rec["stt"].requests == []
        assert snapshot(session_dir, session_id) == before


async def assert_guard(settings, session_dir, payload: bytes, *, status: int, code: str):
    async with audio_client(settings, {}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        response = await upload(client, payload)
        assert_error(response, status=status, code=code)
        assert rec["stt"].requests == []
        assert rec["tts"].requests == []
        assert snapshot(session_dir, session_id) == before


# valid wav empty transcript..valid wav with4001 char transcript: transcript post-conditions ------------------------------


async def assert_transcript_rejected(settings, session_dir, text: str, *, code: str):
    async with audio_client(settings, {"stt": stt_json(text)}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        response = await upload(client, gen_wav.pcm_wav())
        assert_error(response, status=422, code=code)
        assert len(rec["stt"].requests) == 1
        assert snapshot(session_dir, session_id) == before


async def case_valid_wav_empty_transcript(settings, session_dir):
    await assert_transcript_rejected(settings, session_dir, "", code="empty_transcription")


async def case_valid_wav_whitespace_transcript(settings, session_dir):
    await assert_transcript_rejected(
        settings, session_dir, "  \n\t  ", code="empty_transcription"
    )


async def case_valid_wav_with4001_char_transcript(settings, session_dir):
    await assert_transcript_rejected(
        settings, session_dir, "x" * 4001, code="transcript_too_long"
    )


# valid wav stt provider unavailable..valid wav stt timeout: STT service failures ------------------------------------


async def assert_stt_failure(settings, session_dir, responder, *, status: int, code: str):
    async with audio_client(settings, {"stt": responder}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)
        response = await upload(client, gen_wav.pcm_wav())
        assert_error(response, status=status, code=code)
        assert len(rec["stt"].requests) == 2
        assert rec["llm"].requests == [] and rec["tts"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_valid_wav_stt_provider_unavailable(settings, session_dir):
    await assert_stt_failure(
        settings,
        session_dir,
        stt_status(503),
        status=503,
        code="provider_unavailable",
    )


async def case_valid_wav_stt_timeout(settings, session_dir):
    await assert_stt_failure(
        settings, session_dir, stt_timeout(), status=504, code="provider_timeout"
    )


# cancel upload stt task: cancellation -----------------------------------------------------


async def case_cancel_upload_stt_task(settings, session_dir):
    gate = asyncio.Event()
    stream = RecordingStream([b'{"text": "partial', b'"}'], gate)

    def stt_gated(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(
                200, stream=stream, headers={"content-type": "application/json"}
            )
        return json_wire({"text": TRANSCRIPT})

    async with audio_client(settings, {"stt": stt_gated}, capacity=1) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)

        task = asyncio.create_task(upload(client, gen_wav.pcm_wav()))
        await asyncio.wait_for(stream.started.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.closed is True
        assert snapshot(session_dir, session_id) == before

        # The cancelled upload released its permit: a later valid STT succeeds
        # (capacity is 1, so a leaked permit would answer 429 provider_busy).
        response = await upload(client, gen_wav.pcm_wav())
        assert response.status_code == 200
        assert response.json() == {"text": TRANSCRIPT}
        assert len(rec["stt"].requests) == 2
        assert snapshot(session_dir, session_id) == before


CASES = {
    "valid_finite16_khz_mono16_bit_pcm_wav": case_valid_finite16_khz_mono16_bit_pcm_wav,
    "all_zero_valid_wav": case_all_zero_valid_wav,
    "malformed_riff_header_bytes": case_malformed_riff_header_bytes,
    "declared_frames_exceed_actual_bytes": case_declared_frames_exceed_actual_bytes,
    "compressed_wav": case_compressed_wav,
    "webm_payload_file": case_webm_payload_file,
    "valid_pcm_duration61_seconds": case_valid_pcm_duration61_seconds,
    "file10mib_1_bytes": case_file10mib_1_bytes,
    "multipart_total10mib_64kib_1": case_multipart_total10mib_64kib_1,
    "chunked_body_exceeding_multipart_cap_without_content_length": case_chunked_body_exceeding_multipart_cap_without_content_length,
    "valid_wav_empty_transcript": case_valid_wav_empty_transcript,
    "valid_wav_whitespace_transcript": case_valid_wav_whitespace_transcript,
    "valid_wav_with4001_char_transcript": case_valid_wav_with4001_char_transcript,
    "valid_wav_stt_provider_unavailable": case_valid_wav_stt_provider_unavailable,
    "valid_wav_stt_timeout": case_valid_wav_stt_timeout,
    "cancel_upload_stt_task": case_cancel_upload_stt_task,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_transcription(case_id, settings, session_dir):
    await CASES[case_id](settings, session_dir)
