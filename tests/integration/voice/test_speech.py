"""Speech route: saved assistant text and bounded TTS delivery.

Every case runs the real app factory, lifespan, `/api/sessions/{id}/speech`
route, engine message lookup and the real `OpenAICompatibleProvider` TTS adapter
over a per-role `httpx.MockTransport` (IC §13.1). The MP3 bytes served to the
browser are the committed `tests/fixtures/reply.mp3` fixture; the adapter reads
them through the real bounded stream, so content-type, encoding, empty and
5 MiB+1 payloads are judged by shipped code, not by a fake.

Only saved assistant messages may reach TTS: the caller sends a message ID, never
text (arbitrary caller text is post speech caller supplied text extra key). No case mutates the session document.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from gateway.providers.prompts import GREETING
from tests.integration.support.intake import PART_TEXT, start_session, turn_body
from tests.integration.support.transcription import (
    RecordingStream,
    assert_error,
    audio_client,
    json_wire,
    snapshot,
    streamed_wire,
)


MP3_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "reply.mp3"
TTS_PATH = "/v1/audio/speech"
TTS_LIMIT = 5 * 1024 * 1024

PART_EXTRACTION = {
    "intent": "provide_details",
    "has_question": False,
    "candidates": {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    },
    "explicit_corrections": [],
    "ambiguous_fields": [],
"detected_language": "en",
"requested_language": None,
"sentiment": "neutral",
"frustration": "none",
}


def mp3_bytes() -> bytes:
    return MP3_FIXTURE.read_bytes()


def llm_extraction(payload: dict = PART_EXTRACTION):
    content = json.dumps(payload)

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire(
            {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    return responder


def tts_mp3(body: bytes | None = None):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(mp3_bytes() if body is None else body),
            headers={"content-type": "audio/mpeg"},
        )

    return responder


def tts_response(
    chunks: list[bytes],
    *,
    headers: dict[str, str] | None = None,
    gate: asyncio.Event | None = None,
):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        response, stream = streamed_wire(chunks, headers=headers, gate=gate)
        responder.stream = stream
        return response

    responder.stream = None
    return responder


def tts_status(status: int):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return json_wire({"error": "unavailable"}, status=status)

    return responder


def speech_url(session_id: str) -> str:
    return f"/api/sessions/{session_id}/speech"


async def speak(client, session_id: str, message_id: str):
    return await client.post(speech_url(session_id), json={"message_id": message_id})


async def saved_reply(client, session_id: str) -> dict:
    response = await client.post(
        f"/api/sessions/{session_id}/turns", json=turn_body(PART_TEXT, 0)
    )
    assert response.status_code == 200, response.text
    return response.json()


# saved assistant message actual mp3 pcm wav wire fixture -------------------------------------------------------------------


async def case_saved_assistant_message_actual_mp3_pcm_wav_wire_fixture(settings, session_dir):
    async with audio_client(
        settings, {"llm": llm_extraction(), "tts": tts_mp3()}
    ) as (client, rec, _):
        session_id = await start_session(client)
        reply = await saved_reply(client, session_id)
        before = snapshot(session_dir, session_id)

        response = await speak(client, session_id, reply["message_id"])

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert response.content == mp3_bytes()
        assert len(rec["tts"].requests) == 1
        request = rec["tts"].requests[0]
        assert request.url.path == TTS_PATH
        assert request.headers["accept-encoding"] == "identity"
        assert json.loads(request.content) == {
            "model": "test-tts",
            "voice": "test-voice",
            "input": reply["reply"],
            "response_format": "mp3",
        }
        assert snapshot(session_dir, session_id) == before


# absent message uuid..existing user message uuid: only saved assistant messages may be synthesized ---------


async def case_absent_message_uuid(settings, session_dir):
    async with audio_client(settings, {"tts": tts_mp3()}) as (client, rec, _):
        session_id = await start_session(client)
        before = snapshot(session_dir, session_id)

        response = await speak(client, session_id, str(uuid4()))

        assert_error(response, status=404, code="message_not_found")
        assert rec["tts"].requests == []
        assert snapshot(session_dir, session_id) == before


async def case_existing_user_message_uuid(settings, session_dir):
    async with audio_client(
        settings, {"llm": llm_extraction(), "tts": tts_mp3()}
    ) as (client, rec, _):
        session_id = await start_session(client)
        await saved_reply(client, session_id)
        view = (await client.get(f"/api/sessions/{session_id}")).json()
        user_message = next(m for m in view["messages"] if m["role"] == "user")
        before = snapshot(session_dir, session_id)

        response = await speak(client, session_id, user_message["id"])

        assert_error(response, status=404, code="message_not_found")
        assert rec["tts"].requests == []
        assert snapshot(session_dir, session_id) == before


# tts2xx text json content type..tts2xx compressed despite identity: unusable provider responses -----------------------------


async def assert_unusable_tts(settings, session_dir, responder):
    async with audio_client(settings, {"tts": responder}) as (client, rec, _):
        session_id = await start_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        before = snapshot(session_dir, session_id)

        response = await speak(client, session_id, greeting_id)

        assert_error(response, status=502, code="invalid_provider_response")
        assert len(rec["tts"].requests) == 1
        assert snapshot(session_dir, session_id) == before
        return rec


async def case_tts2xx_text_json_content_type(settings, session_dir):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'{"audio": "not an mp3"}'),
            headers={"content-type": "application/json"},
        )

    await assert_unusable_tts(settings, session_dir, responder)


async def case_tts2xx_empty_bytes(settings, session_dir):
    await assert_unusable_tts(settings, session_dir, tts_mp3(body=b""))


async def case_tts2xx_raw_payload5mib_1(settings, session_dir):
    chunks = [b"x" * (1024 * 1024)] * 5 + [b"x"]
    assert sum(len(chunk) for chunk in chunks) == TTS_LIMIT + 1
    responder = tts_response(chunks, headers={"content-type": "audio/mpeg"})
    await assert_unusable_tts(settings, session_dir, responder)
    assert responder.stream.closed is True


async def case_tts2xx_compressed_despite_identity(settings, session_dir):
    compressed = gzip.compress(b"x" * 4096)
    responder = tts_response(
        [compressed],
        headers={"content-type": "audio/mpeg", "content-encoding": "gzip"},
    )
    await assert_unusable_tts(settings, session_dir, responder)
    # No decompression and no body retained: the encoded response is rejected
    # before a single raw byte is handed out.
    assert responder.stream.bytes_read == 0
    assert responder.stream.closed is True


# cancel tts streaming read: cancellation -----------------------------------------------------


async def case_cancel_tts_streaming_read(settings, session_dir):
    gate = asyncio.Event()
    stream = RecordingStream([b"ID3\x04", b"more audio"], gate)

    def tts_gated(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(
                200, stream=stream, headers={"content-type": "audio/mpeg"}
            )
        return httpx.Response(
            200, stream=httpx.ByteStream(mp3_bytes()), headers={"content-type": "audio/mpeg"}
        )

    async with audio_client(settings, {"tts": tts_gated}, capacity=1) as (client, rec, _):
        session_id = await start_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        before = snapshot(session_dir, session_id)

        task = asyncio.create_task(speak(client, session_id, greeting_id))
        await asyncio.wait_for(stream.started.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.closed is True
        assert snapshot(session_dir, session_id) == before

        # Capacity is 1: a later Play only succeeds if the cancelled read
        # released its permit and closed the response.
        response = await speak(client, session_id, greeting_id)
        assert response.status_code == 200
        assert response.content == mp3_bytes()
        assert len(rec["tts"].requests) == 2
        assert snapshot(session_dir, session_id) == before


# tts unavailable on saved reply: TTS unavailable, text intake keeps working -----------------------


async def case_tts_unavailable_on_saved_reply(settings, session_dir):
    async with audio_client(
        settings, {"llm": llm_extraction(), "tts": tts_status(503)}
    ) as (client, rec, _):
        session_id = await start_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        before = snapshot(session_dir, session_id)

        response = await speak(client, session_id, greeting_id)

        assert_error(response, status=503, code="provider_unavailable")
        assert len(rec["tts"].requests) == 1
        assert snapshot(session_dir, session_id) == before

        turn = await client.post(
            f"/api/sessions/{session_id}/turns", json=turn_body(PART_TEXT, 0)
        )
        assert turn.status_code == 200
        assert turn.json()["revision"] == 1
        assert rec["tts"].requests[0].url.path == TTS_PATH


CASES = {
    "saved_assistant_message_actual_mp3_pcm_wav_wire_fixture": case_saved_assistant_message_actual_mp3_pcm_wav_wire_fixture,
    "absent_message_uuid": case_absent_message_uuid,
    "existing_user_message_uuid": case_existing_user_message_uuid,
    "tts2xx_text_json_content_type": case_tts2xx_text_json_content_type,
    "tts2xx_empty_bytes": case_tts2xx_empty_bytes,
    "tts2xx_raw_payload5mib_1": case_tts2xx_raw_payload5mib_1,
    "tts2xx_compressed_despite_identity": case_tts2xx_compressed_despite_identity,
    "cancel_tts_streaming_read": case_cancel_tts_streaming_read,
    "tts_unavailable_on_saved_reply": case_tts_unavailable_on_saved_reply,
}


@pytest.mark.parametrize("case_id", sorted(CASES))
async def test_speech(case_id, settings, session_dir):
    await CASES[case_id](settings, session_dir)
