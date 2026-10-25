"""HTTP routes. Every handler reads its dependencies from ``request.app.state``."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audio import validate_wav
from app.errors import INVALID_REQUEST, app_error
from app.http_support import (
    MAX_MULTIPART_BYTES,
    MAX_SPEECH_TEXT_CHARS,
    SpeechRequest,
    canonical_uuid,
    json_body,
    parse_model,
    read_upload,
    script_safe_json,
    turn_payload,
)
from app.schemas import (
    BookingRequest,
    ConfirmRequest,
    OrderVerificationRequest,
    OrderVerificationResponse,
    SessionView,
    TurnRequest,
    TurnResponse,
)

STATIC_DIR = Path(__file__).parent / "static"
BOOTSTRAP_PLACEHOLDER = "__BOOTSTRAP_LOCALE__"
AUDIO_FIELD = "file"
HEALTHY = {"status": "ok"}

router = APIRouter()


@router.get("/", include_in_schema=False)
async def index(request: Request):
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        raise StarletteHTTPException(status_code=404)
    html = page.read_text(encoding="utf-8")
    bootstrap = script_safe_json(dict(request.app.state.locales.mapping("en")))
    return Response(
        html.replace(BOOTSTRAP_PLACEHOLDER, bootstrap),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/locales/{locale}")
async def locale_dictionary(locale: str, request: Request):
    locales = request.app.state.locales
    if locale not in locales.languages:
        raise StarletteHTTPException(status_code=404)
    return JSONResponse(
        dict(locales.mapping(locale)), headers={"Cache-Control": "no-cache"}
    )


@router.get("/health")
async def health(request: Request):
    await request.app.state.repository.check_writable()
    return JSONResponse(HEALTHY)


@router.post("/api/sessions", status_code=201)
async def create_session(request: Request):
    if json_body(await request.body()) != {}:
        raise app_error(INVALID_REQUEST)
    view: SessionView = await request.app.state.engine.create_session()
    return JSONResponse(view.model_dump(mode="json"), status_code=201)


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    view: SessionView = await request.app.state.engine.get_session(
        canonical_uuid(session_id)
    )
    return JSONResponse(view.model_dump(mode="json"))


@router.post("/api/sessions/{session_id}/turns")
async def turn(session_id: str, request: Request):
    session_uuid = canonical_uuid(session_id)
    command: TurnRequest = parse_model(TurnRequest, json_body(await request.body()))
    response: TurnResponse = await request.app.state.engine.turn(session_uuid, command)
    return JSONResponse(turn_payload(response, request.app.state.speech_languages))


@router.post("/api/sessions/{session_id}/confirm")
async def confirm(session_id: str, request: Request):
    session_uuid = canonical_uuid(session_id)
    command: ConfirmRequest = parse_model(
        ConfirmRequest, json_body(await request.body())
    )
    response: TurnResponse = await request.app.state.engine.confirm(
        session_uuid, command
    )
    return JSONResponse(turn_payload(response, request.app.state.speech_languages))


@router.post("/api/sessions/{session_id}/bookings")
async def book_technician(session_id: str, request: Request):
    session_uuid = canonical_uuid(session_id)
    command: BookingRequest = parse_model(
        BookingRequest, json_body(await request.body())
    )
    response: TurnResponse = await request.app.state.engine.book_slot(
        session_uuid, command
    )
    return JSONResponse(turn_payload(response, request.app.state.speech_languages))


@router.post("/api/sessions/{session_id}/order-verification")
async def order_verification(session_id: str, request: Request):
    session_uuid = canonical_uuid(session_id)
    command: OrderVerificationRequest = parse_model(
        OrderVerificationRequest, json_body(await request.body())
    )
    response: OrderVerificationResponse = await request.app.state.engine.verify_order(
        session_uuid, command
    )
    return JSONResponse(response.model_dump(mode="json"))


@router.post("/api/transcriptions")
async def transcriptions(request: Request):
    async with request.form(max_part_size=MAX_MULTIPART_BYTES) as form:
        uploads = form.getlist(AUDIO_FIELD)
        if (
            set(form.keys()) != {AUDIO_FIELD}
            or len(uploads) != 1
            or not isinstance(uploads[0], UploadFile)
        ):
            raise app_error(INVALID_REQUEST)
        audio = await read_upload(uploads[0])
    validate_wav(audio)
    text = await request.app.state.provider.transcribe(audio)
    return JSONResponse({"text": text})


@router.post("/api/sessions/{session_id}/speech")
async def speech(session_id: str, request: Request):
    session_uuid = canonical_uuid(session_id)
    command: SpeechRequest = parse_model(SpeechRequest, json_body(await request.body()))
    message = await request.app.state.engine.assistant_message(
        session_uuid, command.message_id
    )
    if len(message.text) > MAX_SPEECH_TEXT_CHARS:
        raise app_error("invalid_provider_response")
    if message.language not in request.app.state.speech_languages:
        raise app_error("provider_configuration")
    audio = await request.app.state.provider.synthesize(message.text, message.language)
    return Response(
        content=audio.data,
        media_type=audio.media_type,
        headers={"Cache-Control": "no-store"},
    )
