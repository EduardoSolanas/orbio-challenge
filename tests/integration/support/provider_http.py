"""Real loopback OpenAI-compatible service for provider-boundary integration tests.

The application constructs its shipped ``OpenAICompatibleProvider`` from the
settings below; every request therefore crosses a genuine TCP/HTTP boundary with
the real ``httpx.AsyncClient`` (no transport double, no provider fake). The
server records each request's path, headers and raw body, and each response's
scripted status/body/delay, so tests assert exact wire requests, retry counts and
bounded response consumption. An unexpected path returns 404 and is recorded, so
"no generation call" is provable by request count rather than by inference.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from gateway.config import Settings
from gateway.providers.client import Budget, OpenAICompatibleProvider

CHAT_PATH = "/v1/chat/completions"
SPEECH_PATH = "/v1/audio/speech"
TRANSCRIPTIONS_PATH = "/v1/audio/transcriptions"

PENDING = "pending"
COMPLETED = "completed"
ABORTED = "aborted"
REDIRECTED = "redirected"


@dataclass
class WireSpec:
    """One scripted HTTP response; consumed in order per path."""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None
    delay: float = 0.0
    location: str | None = None


@dataclass
class WireRequest:
    """One observed request and how its response ended."""

    path: str
    headers: dict[str, str]
    body: bytes
    outcome: str = PENDING
    bytes_written: int = 0
    status: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, owner: "LocalProviderServer") -> None:
        super().__init__(address, handler)
        self.owner = owner


class LocalProviderServer:
    """Threaded HTTP server on loopback with per-path scripted responses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, deque[WireSpec]] = {}
        self._requests: list[WireRequest] = []
        self._server = _Server(("127.0.0.1", 0), self._handler(), self)
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

    # -- scripting ----------------------------------------------------------

    def respond(
        self,
        path: str,
        *,
        status: int = 200,
        json_body: object | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        delay: float = 0.0,
        location: str | None = None,
    ) -> None:
        """Queue one response for ``path``; ``chunks`` forces chunked transfer."""

        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        spec = WireSpec(
            status=status,
            body=b"" if body is None else body,
            headers=dict(headers or {}),
            chunks=None if chunks is None else tuple(chunks),
            delay=delay,
            location=location,
        )
        with self._lock:
            self._queues.setdefault(path, deque()).append(spec)

    def script_chat(
        self,
        payload: object,
        *,
        path: str = CHAT_PATH,
        finish_reason: str = "stop",
        **kwargs: Any,
    ) -> None:
        """Queue a chat-completions envelope whose message content is ``payload``."""

        content = payload if isinstance(payload, str) else json.dumps(payload)
        self.respond(
            path,
            json_body={
                "choices": [
                    {"finish_reason": finish_reason, "message": {"content": content}}
                ]
            },
            **kwargs,
        )

    # -- observation --------------------------------------------------------

    def requests(self, path: str | None = None) -> list[WireRequest]:
        with self._lock:
            return [
                request
                for request in self._requests
                if path is None or request.path == path
            ]

    def paths(self) -> list[str]:
        with self._lock:
            return [request.path for request in self._requests]

    def count(self, path: str) -> int:
        return len(self.requests(path))

    def settle(self, path: str, timeout: float = 5.0) -> list[WireRequest]:
        """Wait until every response for ``path`` has finished or aborted."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [r for r in self.requests(path) if r.outcome == PENDING]
            if not pending:
                return self.requests(path)
            time.sleep(0.02)
        return self.requests(path)

    # -- handler ------------------------------------------------------------

    def _record(self, path: str, headers: dict[str, str], body: bytes) -> WireRequest:
        request = WireRequest(
            path=path,
            headers={name.lower(): value for name, value in headers.items()},
            body=body,
        )
        with self._lock:
            self._requests.append(request)
        return request

    def _next(self, path: str) -> WireSpec:
        with self._lock:
            queue = self._queues.get(path)
            if queue:
                return queue.popleft()
        return WireSpec(status=404, body=b'{"error":"unscripted"}')

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # keep pytest output clean
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                record = server._record(self.path, dict(self.headers.items()), raw)
                spec = server._next(self.path)
                record.status = spec.status
                record.response_headers = {
                    name.lower(): value for name, value in spec.headers.items()
                }
                try:
                    if spec.delay:
                        time.sleep(spec.delay)
                    if spec.location is not None:
                        self._start(spec.status, "application/json")
                        self.send_header("location", spec.location)
                        self.send_header("content-length", "0")
                        self.end_headers()
                        record.outcome = REDIRECTED
                        return
                    content_type = spec.headers.get(
                        "content-type", "application/json"
                    )
                    if spec.chunks is not None:
                        self._start(spec.status, content_type)
                        for name, value in spec.headers.items():
                            if name.lower() != "content-type":
                                self.send_header(name, value)
                        self.send_header("transfer-encoding", "chunked")
                        self.end_headers()
                        for chunk in spec.chunks:
                            self.wfile.write(
                                f"{len(chunk):x}\r\n".encode("ascii")
                                + chunk
                                + b"\r\n"
                            )
                            self.wfile.flush()
                            record.bytes_written += len(chunk)
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    else:
                        self._start(spec.status, content_type)
                        for name, value in spec.headers.items():
                            if name.lower() != "content-type":
                                self.send_header(name, value)
                        self.send_header("content-length", str(len(spec.body)))
                        self.end_headers()
                        self.wfile.write(spec.body)
                        self.wfile.flush()
                        record.bytes_written = len(spec.body)
                    record.outcome = COMPLETED
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    record.outcome = ABORTED
                    self.close_connection = True

            def _start(self, status: int, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)

        return Handler


def build_settings(
    session_dir: Path,
    server: LocalProviderServer,
    *,
    output_mode: str = "json_schema",
    auth: str = "bearer",
    llm_key: str = "llm-secret",
    rag_key: str = "rag-secret",
    **overrides: str,
) -> Settings:
    """A complete isolated environment whose four roles point at ``server``."""

    values = {
        "AI_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": server.base_url,
        "LLM_MODEL": "llm-model",
        "LLM_AUTH_MODE": auth,
        "LLM_API_KEY": llm_key if auth == "bearer" else "",
        "LLM_OUTPUT_MODE": output_mode,
        "RAG_BASE_URL": server.base_url,
        "RAG_MODEL": "rag-model",
        "RAG_AUTH_MODE": auth,
        "RAG_API_KEY": rag_key if auth == "bearer" else "",
        "RAG_OUTPUT_MODE": output_mode,
        "STT_BASE_URL": server.base_url,
        "STT_MODEL": "stt-model",
        "STT_AUTH_MODE": "none",
        "TTS_BASE_URL": server.base_url,
        "TTS_MODEL": "tts-model",
        "TTS_AUTH_MODE": "none",
        "TTS_VOICE": "test-voice",
        "SESSION_DIR": str(session_dir),
        "PROVIDER_MAX_CONCURRENCY": "4",
        "BACKEND_API_BASE_URL": "http://backend-api.test",
    }
    values.update(overrides)
    return Settings.from_env(values)


def provider_from(settings: Settings, *, sleep=None) -> OpenAICompatibleProvider:
    """The shipped adapter with one real client per role and no transport double."""

    profiles = {
        "llm": settings.llm,
        "rag": settings.rag,
        "stt": settings.stt,
        "tts": settings.tts,
    }
    if settings.tts_es is not None:
        profiles["tts_es"] = settings.tts_es
    return OpenAICompatibleProvider(
        profiles, capacity=settings.provider_max_concurrency, sleep=sleep
    )


def shorten_text_budget(monkeypatch, *, attempt_timeout: float, total_deadline: float) -> None:
    """Use a smaller real adapter budget so timeout tests stay wall-clock cheap.

    ``extract``/``answer_knowledge`` read ``TEXT_BUDGET`` at call time and the
    role client timeout is built from ``ROLE_BUDGETS``; both are patched before
    the provider is constructed. Retry/repair/validation logic is unchanged.
    """

    import gateway.providers.client as providers

    budget = Budget(2, attempt_timeout, total_deadline)
    monkeypatch.setattr(providers, "TEXT_BUDGET", budget)
    monkeypatch.setitem(providers.ROLE_BUDGETS, "llm", budget)
    monkeypatch.setitem(providers.ROLE_BUDGETS, "rag", budget)


def extraction_payload(
    *,
    intent: str = "provide_details",
    has_question: bool = False,
    candidates: dict[str, str | None] | None = None,
    detected_language: str = "en",
    requested_language: str | None = None,
    sentiment: str = "neutral",
    frustration: str = "none",
) -> dict[str, Any]:
    """A schema-valid extraction body for scripting the real wire response."""

    return {
        "intent": intent,
        "has_question": has_question,
        "candidates": {
            "order_number": None,
            "category": None,
            "description": None,
            "urgency": None,
            **(candidates or {}),
        },
        "explicit_corrections": [],
        "ambiguous_fields": [],
        "detected_language": detected_language,
        "requested_language": requested_language,
        "sentiment": sentiment,
        "frustration": frustration,
    }


def answer_payload(
    *, answerable: bool = True, answer: str = "", cited_ids: list[str] | None = None
) -> dict[str, Any]:
    return {
        "answerable": answerable,
        "answer": answer,
        "cited_ids": list(cited_ids or []),
    }


def chat_user_content(request: WireRequest) -> str:
    """The user message content of a recorded chat-completions request."""

    return request.json()["messages"][1]["content"]


def chat_system_content(request: WireRequest) -> str:
    return request.json()["messages"][0]["content"]
