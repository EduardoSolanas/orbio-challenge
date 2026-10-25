"""HTTP helpers shared by the routes and ASGI guards: parsing, errors, limits."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from backend.gateway.errors import INPUT_TOO_LARGE, INVALID_REQUEST, AppError, app_error
from backend.gateway.schemas import CanonicalUUID, ContractModel, TurnResponse

MAX_JSON_BODY_BYTES = 64 * 1024
MAX_AUDIO_FILE_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_AUDIO_FILE_BYTES + 64 * 1024
MAX_SPEECH_TEXT_CHARS = 4000
UPLOAD_CHUNK_BYTES = 64 * 1024


class SpeechRequest(ContractModel):
    """Speech request: only a saved assistant message ID, never caller text."""

    message_id: CanonicalUUID


def canonical_uuid(value: str) -> UUID:
    """Parse a canonical lowercase UUID path value or map it to invalid_request."""

    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise app_error(INVALID_REQUEST) from None
    if str(parsed) != value:
        raise app_error(INVALID_REQUEST)
    return parsed


def json_body(raw: bytes) -> Any:
    """Parse a JSON request body; empty means the empty object (sessions only)."""

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise app_error(INVALID_REQUEST) from None


def parse_model(model: type[ContractModel], payload: Any) -> Any:
    """Validate a request model, translating oversized text into 413."""

    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        for error in exc.errors():
            if error["loc"] == ("text",) and error["type"] == "string_too_long":
                raise app_error(INPUT_TOO_LARGE) from None
        raise app_error(INVALID_REQUEST) from None


def error_payload(error: AppError, request_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    current_revision = getattr(error, "current_revision", None)
    if current_revision is not None:
        body["current_revision"] = current_revision
    return {"error": body, "request_id": request_id}


def request_id_of(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else str(uuid4())


def script_safe_json(payload: Any) -> str:
    """Serialize JSON for an inline script without letting values close the tag."""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def turn_payload(
    response: TurnResponse, speech_languages: frozenset[str]
) -> dict[str, Any]:
    """Serialize a turn response and add the configuration-derived playback flag.

    ``speech_available`` describes the reply just returned; it is computed here
    from the saved reply language and the operator's current profiles, never
    persisted with the session or its replay records.
    """

    payload = response.model_dump(mode="json")
    payload["speech_available"] = response.reply_language in speech_languages
    return payload


async def read_upload(upload: UploadFile) -> bytes:
    """Read an upload in 64 KiB chunks, rejecting the 10 MiB file cap early."""

    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_AUDIO_FILE_BYTES:
            raise app_error(INPUT_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)
