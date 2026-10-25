"""WAV variants against a real local HTTP server returning WAV bytes.

The app, lifespan, route, engine and ``OpenAICompatibleProvider`` are the shipped
implementations. The external AI service is a real ``http.server`` listening on
loopback, so format negotiation, chunking, status mapping and bounded reads
cross a genuine HTTP boundary with no transport double (IC §13.1).
"""

from __future__ import annotations

import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from gateway.config import Settings
from gateway.main import create_app
from tests.integration.support.api import running_client
from tests.integration.support.transcription import snapshot
from tests.support.fakes import make_streaming_wav, make_wav


TTS_LIMIT = 5 * 1024 * 1024
CHUNK_SECONDS = 0.1
CHUNK_FRAMES = 1600

LONG_DESCRIPTION = (
    "My order arrived broken and the packaging was damaged in several places, "
    "the box was crushed and the item inside was scratched, I would like a "
    "replacement or a refund please."
)

EXTRACTION = {
    "intent": "provide_details",
    "has_question": False,
    "candidates": {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": LONG_DESCRIPTION,
        "urgency": "high",
    },
    "explicit_corrections": [],
    "ambiguous_fields": [],
"detected_language": "en",
"requested_language": None,
"sentiment": "neutral",
"frustration": "none",
}

TURN_TEXT = "My order NS-000123 arrived broken."
TURN_BODY = {
    "request_id": "7a1f2c9e-3b4d-4e5f-8a6b-1c2d3e4f5a6b",
    "expected_revision": 0,
    "text": TURN_TEXT,
    "input_mode": "text",
}


class LocalProviderServer:
    """Real loopback HTTP server: scripted chat completions and speech synthesis."""

    def __init__(self) -> None:
        self.chat_requests: list[dict[str, Any]] = []
        self.speech_requests: list[dict[str, Any]] = []
        self.speech_headers: list[dict[str, str]] = []
        self.speech_status = 200
        self.speech_media_type = "audio/wav"
        self.speech_body: bytes | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "LocalProviderServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # keep pytest output clean
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                if self.path.endswith("/chat/completions"):
                    server.chat_requests.append(json.loads(body))
                    payload = json.dumps(
                        {
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {"content": json.dumps(EXTRACTION)},
                                }
                            ]
                        }
                    ).encode()
                    self._send(200, "application/json", payload)
                    return
                if self.path.endswith("/audio/speech"):
                    server.speech_requests.append(json.loads(body))
                    server.speech_headers.append(dict(self.headers))
                    if server.speech_status != 200:
                        self._send(
                            server.speech_status,
                            "application/json",
                            b'{"error": {"message": "denied"}}',
                        )
                        return
                    audio = server.speech_body
                    if audio is None:
                        audio = make_wav(seconds=CHUNK_SECONDS)
                    self._send(200, server.speech_media_type, audio)
                    return
                self._send(404, "application/json", b"{}")

            def _send(self, status: int, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


def build_settings(session_dir: Path, server: LocalProviderServer, **overrides: str) -> Settings:
    values = {
        "AI_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": server.base_url,
        "LLM_MODEL": "wav-llm",
        "LLM_AUTH_MODE": "none",
        "LLM_API_KEY": "",
        "LLM_OUTPUT_MODE": "json_schema",
        "STT_BASE_URL": server.base_url,
        "STT_MODEL": "wav-stt",
        "STT_AUTH_MODE": "none",
        "STT_API_KEY": "",
        "TTS_BASE_URL": server.base_url,
        "TTS_MODEL": "canopylabs/orpheus-v1-english",
        "TTS_AUTH_MODE": "none",
        "TTS_API_KEY": "",
        "TTS_VOICE": "troy",
        "TTS_AUDIO_FORMAT": "wav",
        "TTS_MAX_INPUT_CHARS": "200",
        "SESSION_DIR": str(session_dir),
        "PROVIDER_MAX_CONCURRENCY": "4",
        "BACKEND_API_BASE_URL": "http://backend-api.test",
    }
    values.update(overrides)
    return Settings.from_env(values)


@pytest.fixture
def server():
    with LocalProviderServer() as active:
        yield active


async def create_session(client) -> str:
    response = await client.post("/api/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def save_reply(client, session_id: str) -> dict:
    response = await client.post(f"/api/sessions/{session_id}/turns", json=TURN_BODY)
    assert response.status_code == 200, response.text
    return response.json()


def wav_frames(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(BytesIO(data)) as reader:
        channels, width, rate = (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
        )
        count = reader.getnframes()
        return channels, width, rate, reader.readframes(count)


async def test_wav_reply_is_chunked_at_the_provider_cap_and_assembled(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server)
    app = create_app(settings=settings)

    async with running_client(app) as client:
        session_id = await create_session(client)
        reply = await save_reply(client, session_id)
        text = reply["reply"]
        assert len(text) > 200

        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": reply["message_id"]}
        )

    expected_chunks = (len(text) + 199) // 200
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert len(server.speech_requests) == expected_chunks
    assert [request["response_format"] for request in server.speech_requests] == ["wav"] * expected_chunks
    assert all(len(request["input"]) <= 200 for request in server.speech_requests)
    assert "".join(request["input"] for request in server.speech_requests) == text
    assert all(request["model"] == "canopylabs/orpheus-v1-english" for request in server.speech_requests)
    assert all(request["voice"] == "troy" for request in server.speech_requests)
    assert all(
        {key.lower(): value for key, value in headers.items()}.get("accept-encoding")
        == "identity"
        for headers in server.speech_headers
    )

    channels, width, rate, frames = wav_frames(response.content)
    assert (channels, width, rate) == (1, 2, 16000)
    assert len(frames) == CHUNK_FRAMES * 2 * expected_chunks
    assert 0 < len(response.content) <= TTS_LIMIT


async def test_mp3_bytes_for_a_wav_profile_are_a_clear_invalid_response(server, tmp_path):
    server.speech_media_type = "audio/mpeg"
    server.speech_body = b"ID3-mp3-bytes"
    settings = build_settings(tmp_path / "sessions", server)
    app = create_app(settings=settings)

    async with running_client(app) as client:
        session_id = await create_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": greeting_id}
        )

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "invalid_provider_response",
        "message": "The AI service returned an unusable response. Please retry.",
        "retryable": True,
    }
    assert len(server.speech_requests) == 1


async def test_provider_rejection_maps_to_a_clear_nonretryable_configuration_error(server, tmp_path):
    server.speech_status = 400
    settings = build_settings(tmp_path / "sessions", server)
    app = create_app(settings=settings)

    async with running_client(app) as client:
        session_id = await create_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": greeting_id}
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "provider_configuration",
        "message": "The AI service configuration needs attention.",
        "retryable": False,
    }
    assert len(server.speech_requests) == 1


async def test_more_than_twenty_chunks_is_rejected_before_any_provider_call(server, tmp_path):
    settings = build_settings(tmp_path / "sessions", server, TTS_MAX_INPUT_CHARS="2")
    app = create_app(settings=settings)

    async with running_client(app) as client:
        session_id = await create_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": greeting_id}
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_configuration"
    assert response.json()["error"]["retryable"] is False
    assert server.speech_requests == []


# Groq Orpheus streaming WAV regression --------------------------------------

STREAMING_RATE = 24000
STREAMING_FRAMES = 2400


async def test_groq_streaming_wav_chunks_are_accepted_and_assembled(server, tmp_path):
    server.speech_body = make_streaming_wav(rate=STREAMING_RATE, frames=STREAMING_FRAMES)
    sessions = tmp_path / "sessions"
    settings = build_settings(sessions, server)
    app = create_app(settings=settings)

    async with running_client(app) as client:
        session_id = await create_session(client)
        reply = await save_reply(client, session_id)
        text = reply["reply"]
        assert len(text) > 200
        before = snapshot(sessions, session_id)

        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": reply["message_id"]}
        )

        assert snapshot(sessions, session_id) == before

    expected_chunks = (len(text) + 199) // 200
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert len(server.speech_requests) == expected_chunks
    assert all(request["response_format"] == "wav" for request in server.speech_requests)

    channels, width, rate, frames = wav_frames(response.content)
    assert (channels, width, rate) == (1, 2, STREAMING_RATE)
    assert len(frames) == STREAMING_FRAMES * 2 * expected_chunks
    assert 0 < len(response.content) <= TTS_LIMIT


async def test_streaming_wav_with_an_incomplete_frame_is_a_clear_invalid_response(
    server, tmp_path
):
    server.speech_body = make_streaming_wav(frames=CHUNK_FRAMES) + b"\x00"
    sessions = tmp_path / "sessions"
    settings = build_settings(sessions, server)
    app = create_app(settings=settings)

    async with running_client(app) as client:
        session_id = await create_session(client)
        greeting_id = (await client.get(f"/api/sessions/{session_id}")).json()["messages"][0]["id"]
        before = snapshot(sessions, session_id)

        response = await client.post(
            f"/api/sessions/{session_id}/speech", json={"message_id": greeting_id}
        )

        assert snapshot(sessions, session_id) == before

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "invalid_provider_response",
        "message": "The AI service returned an unusable response. Please retry.",
        "retryable": True,
    }
    assert len(server.speech_requests) == 1
