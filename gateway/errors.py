"""Fixed error catalogue and startup failure surface.

Every allowed error code, its exact safe message, HTTP status and fixed
retryability live here (implementation contract sections 7 and 11). Messages
are constants: never interpolate exception text, paths, provider bodies, keys
or customer text.
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway.localization import text
from shared.models import ALREADY_BOOKED, SLOT_UNAVAILABLE

SESSION_NOT_FOUND = "session_not_found"
MESSAGE_NOT_FOUND = "message_not_found"
REVISION_CONFLICT = "revision_conflict"
REQUEST_ID_CONFLICT = "request_id_conflict"
STALE_REVIEW = "stale_review"
SESSION_COMPLETED = "session_completed"
SESSION_HANDED_OFF = "session_handed_off"
TURN_LIMIT = "turn_limit"
REVIEW_NOT_READY = "review_not_ready"
INPUT_TOO_LARGE = "input_too_large"
UNSUPPORTED_AUDIO = "unsupported_audio"
INVALID_REQUEST = "invalid_request"
INVALID_AUDIO = "invalid_audio"
SILENT_AUDIO = "silent_audio"
EMPTY_TRANSCRIPTION = "empty_transcription"
TRANSCRIPT_TOO_LONG = "transcript_too_long"
PROVIDER_BUSY = "provider_busy"
SESSION_BUSY = "session_busy"
INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_CONFIGURATION = "provider_configuration"
STORAGE_UNAVAILABLE = "storage_unavailable"
STORAGE_CORRUPT = "storage_corrupt"
PROVIDER_TIMEOUT = "provider_timeout"
TOO_MANY_VERIFICATION_ATTEMPTS = "too_many_verification_attempts"
BACKEND_API_UNAVAILABLE = "backend_api_unavailable"
BACKEND_API_TIMEOUT = "backend_api_timeout"
INVALID_BACKEND_API_RESPONSE = "invalid_backend_api_response"
VERIFICATION_REQUIRED = "verification_required"
INTERNAL_ERROR = "internal_error"

CONFIGURATION_INVALID_MESSAGE = (
    "configuration_invalid: Check required provider and model settings."
)
STORAGE_UNAVAILABLE_MESSAGE = "storage_unavailable: Session storage is unavailable."
KNOWLEDGE_INVALID_MESSAGE = (
    "knowledge_invalid: The bundled knowledge base is missing or invalid."
)
LOCALIZATION_INVALID_MESSAGE = (
    "localization_invalid: The bundled translations are missing or invalid."
)


@dataclass(frozen=True)
class ErrorSpec:
    """One fixed row of the error table."""

    code: str
    message: str
    status: int
    retryable: bool


ERRORS: dict[str, ErrorSpec] = {
    spec.code: spec
    for spec in (
        ErrorSpec(
            SESSION_NOT_FOUND, text("errors.session_not_found", "en"), 404, False
        ),
        ErrorSpec(
            MESSAGE_NOT_FOUND, text("errors.message_not_found", "en"), 404, False
        ),
        ErrorSpec(
            REVISION_CONFLICT,
            text("errors.revision_conflict", "en"),
            409,
            False,
        ),
        ErrorSpec(
            REQUEST_ID_CONFLICT,
            text("errors.request_id_conflict", "en"),
            409,
            False,
        ),
        ErrorSpec(
            STALE_REVIEW,
            text("errors.stale_review", "en"),
            409,
            False,
        ),
        ErrorSpec(
            SESSION_COMPLETED,
            text("errors.session_completed", "en"),
            409,
            False,
        ),
        ErrorSpec(
            SESSION_HANDED_OFF,
            text("errors.session_handed_off", "en"),
            409,
            False,
        ),
        ErrorSpec(
            TURN_LIMIT,
            text("errors.turn_limit", "en"),
            409,
            False,
        ),
        ErrorSpec(
            REVIEW_NOT_READY,
            text("errors.review_not_ready", "en"),
            409,
            False,
        ),
        ErrorSpec(INPUT_TOO_LARGE, text("errors.input_too_large", "en"), 413, False),
        ErrorSpec(
            UNSUPPORTED_AUDIO, text("errors.unsupported_audio", "en"), 415, False
        ),
        ErrorSpec(
            INVALID_REQUEST,
            text("errors.invalid_request", "en"),
            422,
            False,
        ),
        ErrorSpec(
            INVALID_AUDIO,
            text("errors.invalid_audio", "en"),
            422,
            False,
        ),
        ErrorSpec(
            SILENT_AUDIO,
            text("errors.silent_audio", "en"),
            422,
            False,
        ),
        ErrorSpec(
            EMPTY_TRANSCRIPTION,
            text("errors.empty_transcription", "en"),
            422,
            False,
        ),
        ErrorSpec(
            TRANSCRIPT_TOO_LONG,
            text("errors.transcript_too_long", "en"),
            422,
            False,
        ),
        ErrorSpec(PROVIDER_BUSY, text("errors.provider_busy", "en"), 429, True),
        ErrorSpec(
            SESSION_BUSY,
            text("errors.session_busy", "en"),
            429,
            True,
        ),
        ErrorSpec(
            INVALID_PROVIDER_RESPONSE,
            text("errors.invalid_provider_response", "en"),
            502,
            True,
        ),
        ErrorSpec(
            PROVIDER_UNAVAILABLE,
            text("errors.provider_unavailable", "en"),
            503,
            True,
        ),
        ErrorSpec(
            PROVIDER_CONFIGURATION,
            text("errors.provider_configuration", "en"),
            503,
            False,
        ),
        ErrorSpec(
            STORAGE_UNAVAILABLE,
            text("errors.storage_unavailable", "en"),
            503,
            True,
        ),
        ErrorSpec(
            STORAGE_CORRUPT,
            text("errors.storage_corrupt", "en"),
            503,
            False,
        ),
        ErrorSpec(PROVIDER_TIMEOUT, text("errors.provider_timeout", "en"), 504, True),
        ErrorSpec(
            TOO_MANY_VERIFICATION_ATTEMPTS,
            text("errors.too_many_verification_attempts", "en"),
            429,
            True,
        ),
        ErrorSpec(
            BACKEND_API_UNAVAILABLE,
            text("errors.backend_api_unavailable", "en"),
            503,
            True,
        ),
        ErrorSpec(
            BACKEND_API_TIMEOUT,
            text("errors.backend_api_timeout", "en"),
            504,
            True,
        ),
        ErrorSpec(
            INVALID_BACKEND_API_RESPONSE,
            text("errors.invalid_backend_api_response", "en"),
            502,
            True,
        ),
        ErrorSpec(SLOT_UNAVAILABLE, text("errors.slot_unavailable", "en"), 409, False),
        ErrorSpec(ALREADY_BOOKED, text("errors.already_booked", "en"), 409, False),
        ErrorSpec(
            VERIFICATION_REQUIRED,
            text("errors.verification_required", "en"),
            403,
            False,
        ),
        ErrorSpec(INTERNAL_ERROR, text("errors.internal_error", "en"), 500, False),
    )
}


class AppError(Exception):
    """Application error carrying the fixed code, safe message and status."""

    def __init__(self, code: str, message: str, status: int, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


def app_error(code: str) -> AppError:
    """Build an AppError from the fixed table row for ``code``."""

    spec = ERRORS[code]
    return AppError(spec.code, spec.message, spec.status, spec.retryable)
