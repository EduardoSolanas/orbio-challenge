"""Shared integration harness for the real app: settings, lifespan, provider, raw ASGI.

Only the external AI service is doubled (`ScriptedProvider`); the app, lifespan,
routes, engine and JSON repository are the real implementations running against
an isolated temporary directory (IC §13.1).
"""

from __future__ import annotations

import json
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.providers import SynthesizedAudio
from app.schemas import Extraction, KnowledgeAnswer, Slots

NO_SLOTS: dict[str, str | None] = {
    "order_number": None,
    "category": None,
    "description": None,
    "urgency": None,
}


def make_extraction(
    *,
    intent: str = "provide_details",
    has_question: bool = False,
    candidates: dict[str, str | None] | None = None,
    explicit_corrections: Iterable[str] = (),
    ambiguous_fields: Iterable[str] = (),
) -> Extraction:
    """Build a strict extraction object for scripting the fake provider."""

    return Extraction(
        intent=intent,
        has_question=has_question,
        candidates=Slots(**{**NO_SLOTS, **(candidates or {})}),
        explicit_corrections=list(explicit_corrections),
        ambiguous_fields=list(ambiguous_fields),
    )


def unrelated_extraction() -> Extraction:
    return make_extraction(intent="unrelated")


def full_extraction() -> Extraction:
    return make_extraction(
        candidates={
            "order_number": "NS-000123",
            "category": "damaged_item",
            "description": "My order arrived broken",
            "urgency": "high",
        }
    )


def build_settings(session_dir: Path) -> Settings:
    """A complete isolated auth=none environment; no developer configuration is read."""

    return Settings.from_env(
        {
            "AI_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_MODEL": "test-llm",
            "LLM_AUTH_MODE": "none",
            "LLM_OUTPUT_MODE": "json_schema",
            "STT_BASE_URL": "http://127.0.0.1:9/v1",
            "STT_MODEL": "test-stt",
            "STT_AUTH_MODE": "none",
            "TTS_BASE_URL": "http://127.0.0.1:9/v1",
            "TTS_MODEL": "test-tts",
            "TTS_AUTH_MODE": "none",
            "TTS_VOICE": "test-voice",
            "SESSION_DIR": str(session_dir),
            "PROVIDER_MAX_CONCURRENCY": "4",
        }
    )


class ScriptedProvider:
    """Explicitly scripted AIProvider; an unexpected call is a test failure."""

    def __init__(self) -> None:
        self.extractions: deque[Extraction] = deque()
        self.answers: deque[KnowledgeAnswer] = deque()
        self.transcriptions: deque[str] = deque()
        self.speech: deque[SynthesizedAudio] = deque()
        self.calls: list[tuple[str, Any]] = []
        self.close_count = 0

    def script_extraction(self, extraction: Extraction) -> None:
        self.extractions.append(extraction)

    def script_answer(self, answer: KnowledgeAnswer) -> None:
        self.answers.append(answer)

    def script_transcription(self, text: str) -> None:
        self.transcriptions.append(text)

    def script_speech(self, audio: bytes, media_type: str = "audio/mpeg") -> None:
        self.speech.append(SynthesizedAudio(audio, media_type))

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def extract(self, context):
        self.calls.append(("extract", context))
        if not self.extractions:
            raise AssertionError("unexpected extract() call")
        return self.extractions.popleft()

    async def answer_knowledge(self, context):
        self.calls.append(("answer_knowledge", context))
        if not self.answers:
            raise AssertionError("unexpected answer_knowledge() call")
        return self.answers.popleft()

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(("transcribe", audio))
        if not self.transcriptions:
            raise AssertionError("unexpected transcribe() call")
        return self.transcriptions.popleft()

    async def synthesize(self, text: str) -> SynthesizedAudio:
        self.calls.append(("synthesize", text))
        if not self.speech:
            raise AssertionError("unexpected synthesize() call")
        return self.speech.popleft()

    async def close(self) -> None:
        self.close_count += 1


@asynccontextmanager
async def running_client(app):
    """Run the real app lifespan, then talk to it through a real ASGI client."""

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


async def raw_asgi(
    app,
    *,
    method: str,
    path: str,
    chunks: list[bytes],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Drive the app with an explicitly framed ASGI receive sequence."""

    header_items = [(b"host", b"testserver")]
    for name, value in (headers or {}).items():
        header_items.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": header_items,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    pending = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], dict(start.get("headers", [])), body


def session_files(session_dir: Path) -> list[Path]:
    return sorted(Path(session_dir).glob("*.json"))


def read_session(session_dir: Path, session_id: str) -> dict[str, Any]:
    return json.loads((Path(session_dir) / f"{session_id}.json").read_text("utf-8"))


def canonical_uuid(value: str) -> UUID:
    return UUID(value)
