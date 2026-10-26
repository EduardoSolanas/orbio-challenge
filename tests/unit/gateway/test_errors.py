"""Unit tests for the fixed error catalogue and startup failure surface.

Requirements: IC section 7 (error codes/status) and IC section 11 (exact safe
messages, retryability and startup RuntimeError strings).
"""

import dataclasses

import pytest

from gateway import errors
from gateway.errors import AppError, ErrorSpec, app_error

# code -> (message, status, retryable), transcribed from IC section 11.
EXPECTED = {
    "session_not_found": ("Session not found.", 404, False),
    "message_not_found": ("Assistant message not found.", 404, False),
    "revision_conflict": (
        "This session changed. Refresh before sending again.",
        409,
        False,
    ),
    "request_id_conflict": (
        "This request ID was already used for different input.",
        409,
        False,
    ),
    "stale_review": (
        "The review changed. Review the latest details before confirming.",
        409,
        False,
    ),
    "session_completed": (
        "This support conversation is complete. Start a new conversation if you need help with another issue.",
        409,
        False,
    ),
    "session_handed_off": (
        "This conversation has been handed off in the demo. Start a new conversation if you need more help.",
        409,
        False,
    ),
    "turn_limit": (
        "This conversation has reached its message limit. You can confirm the current details or start a new conversation.",
        409,
        False,
    ),
    "review_not_ready": (
        "Resolve missing or unclear details before confirming.",
        409,
        False,
    ),
    "input_too_large": ("The input exceeds the allowed size.", 413, False),
    "unsupported_audio": ("Upload a supported PCM WAV recording.", 415, False),
    "invalid_request": ("The request does not match the expected format.", 422, False),
    "invalid_audio": (
        "The WAV recording is invalid or exceeds the audio limits.",
        422,
        False,
    ),
    "silent_audio": (
        "No speech was detected. Try another recording or type your message.",
        422,
        False,
    ),
    "empty_transcription": (
        "No transcript was returned. Try another recording or type your message.",
        422,
        False,
    ),
    "transcript_too_long": (
        "The transcript exceeds 4000 characters. Use a shorter recording.",
        422,
        False,
    ),
    "provider_busy": ("The AI service is busy. Please retry.", 429, True),
    "session_busy": (
        "This session is processing another request. Please retry.",
        429,
        True,
    ),
    "invalid_provider_response": (
        "The AI service returned an unusable response. Please retry.",
        502,
        True,
    ),
    "provider_unavailable": ("The AI service is unavailable. Please retry.", 503, True),
    "provider_configuration": (
        "The AI service configuration needs attention.",
        503,
        False,
    ),
    "storage_unavailable": ("Session storage is unavailable. Please retry.", 503, True),
    "storage_corrupt": (
        "This session cannot be loaded. Contact the demo operator.",
        503,
        False,
    ),
    "provider_timeout": ("The AI request timed out. Please retry.", 504, True),
    "too_many_verification_attempts": (
        "Too many verification attempts. Start a new conversation or restart the demo to try again.",
        429,
        True,
    ),
    "backend_api_unavailable": (
        "The backend service is unavailable. Please retry.",
        503,
        True,
    ),
    "backend_api_timeout": ("The backend service timed out. Please retry.", 504, True),
    "invalid_backend_api_response": (
        "The backend service returned an unusable response. Please retry.",
        502,
        True,
    ),
    "slot_unavailable": (
        "That time was just taken. Ask for technician times again to see what is free.",
        409,
        False,
    ),
    "already_booked": (
        "This account already has a technician visit booked.",
        409,
        False,
    ),
    "verification_required": (
        "Verify the energy account before booking a technician.",
        403,
        False,
    ),
    "internal_error": ("An internal error occurred.", 500, False),
}


def test_error_table_covers_exactly_the_contract_codes():
    assert set(errors.ERRORS) == set(EXPECTED)


@pytest.mark.parametrize("code", sorted(EXPECTED))
def test_error_spec_matches_contract(code):
    message, status, retryable = EXPECTED[code]
    spec = errors.ERRORS[code]

    assert isinstance(spec, ErrorSpec)
    assert spec.code == code
    assert spec.message == message
    assert spec.status == status
    assert spec.retryable is retryable
    assert getattr(errors, code.upper()) == code


@pytest.mark.parametrize("code", sorted(EXPECTED))
def test_app_error_factory_uses_the_contract_row(code):
    message, status, retryable = EXPECTED[code]
    exc = app_error(code)

    assert isinstance(exc, AppError)
    assert isinstance(exc, Exception)
    assert exc.code == code
    assert exc.message == message
    assert exc.status == status
    assert exc.retryable is retryable
    assert str(exc) == message


def test_app_error_accepts_an_explicit_status():
    exc = AppError("custom_code", "Custom message.", 418, False)

    assert exc.code == "custom_code"
    assert exc.message == "Custom message."
    assert exc.status == 418
    assert exc.retryable is False
    assert str(exc) == "Custom message."


def test_error_specs_are_frozen():
    spec = errors.ERRORS["session_not_found"]

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.status = 500


def test_startup_failure_messages_are_exact():
    assert (
        errors.CONFIGURATION_INVALID_MESSAGE
        == "configuration_invalid: Check required provider and model settings."
    )
    assert (
        errors.STORAGE_UNAVAILABLE_MESSAGE
        == "storage_unavailable: Session storage is unavailable."
    )
    assert (
        errors.KNOWLEDGE_INVALID_MESSAGE
        == "knowledge_invalid: The bundled knowledge base is missing or invalid."
    )
