"""Fixed error catalogue and startup failure surface.

Every allowed error code, its exact safe message, HTTP status and fixed
retryability live here (implementation contract sections 7 and 11). Messages
are constants: never interpolate exception text, paths, provider bodies, keys
or customer text.
"""

from __future__ import annotations

from dataclasses import dataclass

SESSION_NOT_FOUND = "session_not_found"
MESSAGE_NOT_FOUND = "message_not_found"
REVISION_CONFLICT = "revision_conflict"
REQUEST_ID_CONFLICT = "request_id_conflict"
STALE_REVIEW = "stale_review"
SESSION_COMPLETED = "session_completed"
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
INTERNAL_ERROR = "internal_error"

CONFIGURATION_INVALID_MESSAGE = (
    "configuration_invalid: Check required provider and model settings."
)
STORAGE_UNAVAILABLE_MESSAGE = "storage_unavailable: Session storage is unavailable."
KNOWLEDGE_INVALID_MESSAGE = (
    "knowledge_invalid: The bundled knowledge base is missing or invalid."
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
        ErrorSpec(SESSION_NOT_FOUND, "Session not found.", 404, False),
        ErrorSpec(MESSAGE_NOT_FOUND, "Assistant message not found.", 404, False),
        ErrorSpec(
            REVISION_CONFLICT,
            "This session changed. Refresh before sending again.",
            409,
            False,
        ),
        ErrorSpec(
            REQUEST_ID_CONFLICT,
            "This request ID was already used for different input.",
            409,
            False,
        ),
        ErrorSpec(
            STALE_REVIEW,
            "The review changed. Review the latest details before confirming.",
            409,
            False,
        ),
        ErrorSpec(
            SESSION_COMPLETED,
            "This intake is complete. Start a new intake to make changes.",
            409,
            False,
        ),
        ErrorSpec(
            TURN_LIMIT,
            "This intake has reached its message limit. You can confirm an existing review or start a new intake.",
            409,
            False,
        ),
        ErrorSpec(
            REVIEW_NOT_READY,
            "Resolve missing or unclear details before confirming.",
            409,
            False,
        ),
        ErrorSpec(INPUT_TOO_LARGE, "The input exceeds the allowed size.", 413, False),
        ErrorSpec(UNSUPPORTED_AUDIO, "Upload a supported PCM WAV recording.", 415, False),
        ErrorSpec(
            INVALID_REQUEST,
            "The request does not match the expected format.",
            422,
            False,
        ),
        ErrorSpec(
            INVALID_AUDIO,
            "The WAV recording is invalid or exceeds the audio limits.",
            422,
            False,
        ),
        ErrorSpec(
            SILENT_AUDIO,
            "No speech was detected. Try another recording or type your message.",
            422,
            False,
        ),
        ErrorSpec(
            EMPTY_TRANSCRIPTION,
            "No transcript was returned. Try another recording or type your message.",
            422,
            False,
        ),
        ErrorSpec(
            TRANSCRIPT_TOO_LONG,
            "The transcript exceeds 4000 characters. Use a shorter recording.",
            422,
            False,
        ),
        ErrorSpec(PROVIDER_BUSY, "The AI service is busy. Please retry.", 429, True),
        ErrorSpec(
            SESSION_BUSY,
            "This session is processing another request. Please retry.",
            429,
            True,
        ),
        ErrorSpec(
            INVALID_PROVIDER_RESPONSE,
            "The AI service returned an unusable response. Please retry.",
            502,
            True,
        ),
        ErrorSpec(
            PROVIDER_UNAVAILABLE,
            "The AI service is unavailable. Please retry.",
            503,
            True,
        ),
        ErrorSpec(
            PROVIDER_CONFIGURATION,
            "The AI service configuration needs attention.",
            503,
            False,
        ),
        ErrorSpec(
            STORAGE_UNAVAILABLE,
            "Session storage is unavailable. Please retry.",
            503,
            True,
        ),
        ErrorSpec(
            STORAGE_CORRUPT,
            "This session cannot be loaded. Contact the demo operator.",
            503,
            False,
        ),
        ErrorSpec(PROVIDER_TIMEOUT, "The AI request timed out. Please retry.", 504, True),
        ErrorSpec(INTERNAL_ERROR, "An internal error occurred.", 500, False),
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
