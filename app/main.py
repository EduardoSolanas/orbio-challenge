"""FastAPI app factory, lifespan assembly, routes and ASGI request guards.

`create_app(settings=None, provider=None, knowledge_path=None)` assembles the
real dependency graph on lifespan start (IC §3): settings validation, writable
session storage, repository, provider, immutable knowledge base, engine. Every
route calls the engine from `request.app.state.engine`; the speech route uses the
provider directly after a saved-message lookup and never mutates sessions.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audio import validate_wav
from app.config import Settings
from app.conversation import ConversationEngine
from app.errors import (
    AppError,
    INPUT_TOO_LARGE,
    INVALID_REQUEST,
    KNOWLEDGE_INVALID_MESSAGE,
    STORAGE_UNAVAILABLE_MESSAGE,
    app_error,
)
from app.providers import OpenAICompatibleProvider
from app.retrieval import KnowledgeInvalid, load_knowledge
from app.schemas import (
    CanonicalUUID,
    ConfirmRequest,
    ContractModel,
    SessionView,
    TurnRequest,
    TurnResponse,
)
from app.storage import JsonSessionRepository

logger = logging.getLogger("app.main")

MAX_JSON_BODY_BYTES = 64 * 1024
MAX_AUDIO_FILE_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_AUDIO_FILE_BYTES + 64 * 1024
MAX_SPEECH_TEXT_CHARS = 4000
AUDIO_FIELD = "file"
DEFAULT_KNOWLEDGE_PATH = Path(__file__).parent / "knowledge" / "northstar-faq.json"
STATIC_DIR = Path(__file__).parent / "static"
HEALTHY = {"status": "ok"}


def _canonical_uuid(value: str) -> UUID:
    """Parse a canonical lowercase UUID path value or map it to invalid_request."""

    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise app_error(INVALID_REQUEST) from None
    if str(parsed) != value:
        raise app_error(INVALID_REQUEST)
    return parsed


def _json_body(raw: bytes) -> Any:
    """Parse a JSON request body; empty means the empty object (sessions only)."""

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise app_error(INVALID_REQUEST) from None


def _parse(model: type[ContractModel], payload: Any) -> Any:
    """Validate a request model, translating oversized text into 413."""

    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        for error in exc.errors():
            if error["loc"] == ("text",) and error["type"] == "string_too_long":
                raise app_error(INPUT_TOO_LARGE) from None
        raise app_error(INVALID_REQUEST) from None


def _error_payload(error: AppError, request_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    current_revision = getattr(error, "current_revision", None)
    if current_revision is not None:
        body["current_revision"] = current_revision
    return {"error": body, "request_id": request_id}


def _request_id_of(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else str(uuid4())


async def _send_error(send, error: AppError, request_id: str) -> None:
    body = json.dumps(_error_payload(error, request_id)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": error.status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header_value(scope, name: bytes) -> str | None:
    for header_name, header_value in scope["headers"]:
        if header_name.lower() == name:
            return header_value.decode("latin-1")
    return None


def _valid_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


class CorrelationIdMiddleware:
    """Resolve/echo X-Request-ID and log only sanitized request facts (IC §7)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _valid_request_id(_header_value(scope, b"x-request-id")) or str(
            uuid4()
        )
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 0

        async def send_with_header(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            logger.info(
                "request_id=%s method=%s path=%s status=%s duration_ms=%d",
                request_id,
                scope["method"],
                scope["path"],
                status,
                int((time.monotonic() - started) * 1000),
            )


class JsonBodyGuardMiddleware:
    """Pure ASGI preparse guard: count actual receive bytes on every POST.

    JSON routes keep the 64 KiB cap; the transcription upload keeps its separate
    10 MiB file / 10 MiB + 64 KiB multipart caps (IC §7, §12). Content-Length is
    never trusted; a single ASGI message larger than the cap is rejected before
    it is retained.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        limit = (
            MAX_MULTIPART_BYTES
            if path == "/api/transcriptions"
            else MAX_JSON_BODY_BYTES
        )

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                request_id = scope.get("state", {}).get("request_id") or str(uuid4())
                await _send_error(send, app_error(INPUT_TOO_LARGE), request_id)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        joined = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": joined, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _read_upload(upload: UploadFile) -> bytes:
    """Read an upload in 64 KiB chunks, rejecting the 10 MiB file cap early."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_AUDIO_FILE_BYTES:
            raise app_error(INPUT_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


class SpeechRequest(ContractModel):
    """Speech request: only a saved assistant message ID, never caller text."""

    message_id: CanonicalUUID


def create_app(
    settings: Settings | None = None,
    provider=None,
    knowledge_path: Path | None = None,
) -> FastAPI:
    """Build the app; dependencies are assembled on lifespan start (IC §3)."""

    kb_path = DEFAULT_KNOWLEDGE_PATH if knowledge_path is None else Path(knowledge_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_settings = Settings.from_env() if settings is None else settings
        session_dir = Path(active_settings.session_dir)
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            repository = JsonSessionRepository(session_dir)
            await repository.check_writable()
        except (OSError, AppError):
            # Resources already constructed for this lifespan (an injected
            # provider) are closed before the startup failure surfaces.
            if provider is not None:
                await provider.close()
            raise RuntimeError(STORAGE_UNAVAILABLE_MESSAGE) from None

        active_provider = provider
        if active_provider is None:
            active_provider = OpenAICompatibleProvider(
                {
                    "llm": active_settings.llm,
                    "rag": active_settings.rag,
                    "stt": active_settings.stt,
                    "tts": active_settings.tts,
                },
                capacity=active_settings.provider_max_concurrency,
            )
        try:
            knowledge_base = load_knowledge(kb_path)
        except KnowledgeInvalid:
            await active_provider.close()
            raise RuntimeError(KNOWLEDGE_INVALID_MESSAGE) from None

        app.state.settings = active_settings
        app.state.repository = repository
        app.state.provider = active_provider
        app.state.knowledge_base = knowledge_base
        app.state.engine = ConversationEngine(
            repository, active_provider, knowledge_base
        )
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await active_provider.close()

    app = FastAPI(title="Northstar support intake", lifespan=lifespan)
    app.add_middleware(JsonBodyGuardMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        request_id = _request_id_of(request)
        logger.info(
            "request_id=%s error_code=%s status=%s", request_id, exc.code, exc.status
        )
        return JSONResponse(
            _error_payload(exc, request_id), status_code=exc.status
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        request_id = _request_id_of(request)
        logger.error(
            "request_id=%s exception=%s", request_id, type(exc).__name__
        )
        return JSONResponse(
            _error_payload(app_error("internal_error"), request_id),
            status_code=500,
            headers={"x-request-id": request_id},
        )

    @app.get("/", include_in_schema=False)
    async def index():
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise StarletteHTTPException(status_code=404)
        return FileResponse(page)

    @app.get("/health")
    async def health(request: Request):
        await request.app.state.repository.check_writable()
        return JSONResponse(HEALTHY)

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request):
        payload = _json_body(await request.body())
        if payload != {}:
            raise app_error(INVALID_REQUEST)
        view: SessionView = await request.app.state.engine.create_session()
        return JSONResponse(view.model_dump(mode="json"), status_code=201)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        view: SessionView = await request.app.state.engine.get_session(
            _canonical_uuid(session_id)
        )
        return JSONResponse(view.model_dump(mode="json"))

    @app.post("/api/sessions/{session_id}/turns")
    async def turn(session_id: str, request: Request):
        session_uuid = _canonical_uuid(session_id)
        command: TurnRequest = _parse(
            TurnRequest, _json_body(await request.body())
        )
        response: TurnResponse = await request.app.state.engine.turn(
            session_uuid, command
        )
        return JSONResponse(response.model_dump(mode="json"))

    @app.post("/api/sessions/{session_id}/confirm")
    async def confirm(session_id: str, request: Request):
        session_uuid = _canonical_uuid(session_id)
        command: ConfirmRequest = _parse(
            ConfirmRequest, _json_body(await request.body())
        )
        response: TurnResponse = await request.app.state.engine.confirm(
            session_uuid, command
        )
        return JSONResponse(response.model_dump(mode="json"))

    @app.post("/api/transcriptions")
    async def transcriptions(request: Request):
        async with request.form(max_part_size=MAX_MULTIPART_BYTES) as form:
            uploads = form.getlist(AUDIO_FIELD)
            if (
                set(form.keys()) != {AUDIO_FIELD}
                or len(uploads) != 1
                or not isinstance(uploads[0], UploadFile)
            ):
                raise app_error(INVALID_REQUEST)
            audio = await _read_upload(uploads[0])
        validate_wav(audio)
        text = await request.app.state.provider.transcribe(audio)
        return JSONResponse({"text": text})

    @app.post("/api/sessions/{session_id}/speech")
    async def speech(session_id: str, request: Request):
        session_uuid = _canonical_uuid(session_id)
        command: SpeechRequest = _parse(
            SpeechRequest, _json_body(await request.body())
        )
        text = await request.app.state.engine.assistant_text(
            session_uuid, command.message_id
        )
        if len(text) > MAX_SPEECH_TEXT_CHARS:
            raise app_error("invalid_provider_response")
        audio = await request.app.state.provider.synthesize(text)
        return Response(
            content=audio.data,
            media_type=audio.media_type,
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
