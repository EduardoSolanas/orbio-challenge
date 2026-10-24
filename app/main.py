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
from fastapi.responses import JSONResponse, Response
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
    LOCALIZATION_INVALID_MESSAGE,
    STORAGE_UNAVAILABLE_MESSAGE,
    app_error,
)
from app.localization import LOCALES_DIR, LocalizationInvalid, load_locales
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
BOOTSTRAP_PLACEHOLDER = "__BOOTSTRAP_LOCALE__"
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


def _script_safe_json(payload: Any) -> str:
    """Serialize JSON for an inline script without letting values close the tag."""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


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


def _turn_payload(response: TurnResponse, speech_languages: frozenset[str]) -> dict[str, Any]:
    """Serialize a turn response and add the configuration-derived playback flag.

    ``speech_available`` describes the reply just returned; it is computed here
    from the saved reply language and the operator's current profiles, never
    persisted with the session or its replay records.
    """

    payload = response.model_dump(mode="json")
    payload["speech_available"] = response.reply_language in speech_languages
    return payload


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
    locales_dir: Path | None = None,
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
            profiles = {
                "llm": active_settings.llm,
                "rag": active_settings.rag,
                "stt": active_settings.stt,
                "tts": active_settings.tts,
            }
            if active_settings.tts_es is not None:
                profiles["tts_es"] = active_settings.tts_es
            active_provider = OpenAICompatibleProvider(
                profiles,
                capacity=active_settings.provider_max_concurrency,
            )
        try:
            locales = load_locales(locales_dir)
        except LocalizationInvalid:
            await active_provider.close()
            raise RuntimeError(LOCALIZATION_INVALID_MESSAGE) from None
        try:
            knowledge_base = load_knowledge(kb_path)
        except KnowledgeInvalid:
            await active_provider.close()
            raise RuntimeError(KNOWLEDGE_INVALID_MESSAGE) from None

        speech_languages = frozenset({"en"})
        if active_settings.tts_es is not None:
            speech_languages = speech_languages | {"es"}

        app.state.settings = active_settings
        app.state.locales = locales
        app.state.repository = repository
        app.state.provider = active_provider
        app.state.knowledge_base = knowledge_base
        app.state.speech_languages = speech_languages
        app.state.engine = ConversationEngine(
            repository, active_provider, knowledge_base, speech_languages=speech_languages
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

    @app.middleware("http")
    async def revalidate_document_and_assets(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise StarletteHTTPException(status_code=404)
        html = page.read_text(encoding="utf-8")
        bootstrap = _script_safe_json(dict(request.app.state.locales.mapping("en")))
        return Response(
            html.replace(BOOTSTRAP_PLACEHOLDER, bootstrap),
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/locales/{locale}")
    async def locale_dictionary(locale: str, request: Request):
        locales = request.app.state.locales
        if locale not in locales.languages:
            raise StarletteHTTPException(status_code=404)
        return JSONResponse(
            dict(locales.mapping(locale)), headers={"Cache-Control": "no-cache"}
        )

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
        return JSONResponse(_turn_payload(response, request.app.state.speech_languages))

    @app.post("/api/sessions/{session_id}/confirm")
    async def confirm(session_id: str, request: Request):
        session_uuid = _canonical_uuid(session_id)
        command: ConfirmRequest = _parse(
            ConfirmRequest, _json_body(await request.body())
        )
        response: TurnResponse = await request.app.state.engine.confirm(
            session_uuid, command
        )
        return JSONResponse(_turn_payload(response, request.app.state.speech_languages))

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
        message = await request.app.state.engine.assistant_message(
            session_uuid, command.message_id
        )
        text = message.text
        if len(text) > MAX_SPEECH_TEXT_CHARS:
            raise app_error("invalid_provider_response")
        if message.language not in request.app.state.speech_languages:
            raise app_error("provider_configuration")
        audio = await request.app.state.provider.synthesize(text, message.language)
        return Response(
            content=audio.data,
            media_type=audio.media_type,
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
