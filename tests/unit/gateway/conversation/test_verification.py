"""Unit tests for the gateway-side verification registry.

The Backend API owns the credential rule and fixture (`tests/unit/backend_api/test_backend_api.py`).
This registry only tracks the pending order, failure count and 15-minute grant per
session; these tests pin binding, expiry, lockout and every reset path.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from gateway.conversation.verification import (
    MAX_VERIFICATION_ATTEMPTS,
    VERIFICATION_TTL_SECONDS,
    VerificationRegistry,
)
from gateway.localization import text
from gateway.schemas import (
    Extraction,
    OrderVerificationRequest,
    Slots,
    ToolRequest,
)

FIXTURE_ORDER_NUMBER = "NS-000123"
OTHER_ORDER = "NS-000999"


class FakeClock:
    """Deterministic monotonic clock for expiry tests."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def registry(clock: FakeClock | None = None) -> VerificationRegistry:
    return VerificationRegistry(clock=clock or FakeClock())


def extraction(**overrides) -> Extraction:
    values = {
        "intent": "order_lookup",
        "has_question": True,
        "candidates": Slots(
            order_number=None, category=None, description=None, urgency=None
        ),
        "explicit_corrections": [],
        "ambiguous_fields": [],
        "detected_language": "en",
        "requested_language": None,
        "sentiment": "neutral",
        "frustration": "none",
    }
    values.update(overrides)
    return Extraction(**values)


def test_grant_is_bound_to_the_session_and_the_verified_order() -> None:
    verifier = registry()
    first, second = uuid4(), uuid4()
    verifier.set_pending_order(first, FIXTURE_ORDER_NUMBER)
    verifier.set_pending_order(second, FIXTURE_ORDER_NUMBER)

    verifier.record_success(first, FIXTURE_ORDER_NUMBER)

    assert verifier.grant(first) == FIXTURE_ORDER_NUMBER
    assert verifier.grant(second) is None
    assert verifier.grant(uuid4()) is None


def test_grant_expires_after_fifteen_minutes() -> None:
    clock = FakeClock()
    verifier = registry(clock)
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    assert VERIFICATION_TTL_SECONDS == 15 * 60
    clock.advance(VERIFICATION_TTL_SECONDS - 1)
    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER
    clock.advance(1)
    assert verifier.grant(session_id) is None
    # Only the grant expires; the pending lookup order survives for reverification.
    assert verifier.pending_order(session_id) == FIXTURE_ORDER_NUMBER
    assert verifier.failures(session_id) == 0


def test_success_at_the_exact_expiry_issues_a_fresh_grant() -> None:
    clock = FakeClock()
    verifier = registry(clock)
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    clock.advance(VERIFICATION_TTL_SECONDS)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    clock.advance(1)
    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER


def test_repeating_a_successful_verification_preserves_the_original_expiry() -> None:
    clock = FakeClock()
    verifier = registry(clock)
    session_id = uuid4()
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    clock.advance(600)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    clock.advance(299)
    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER
    clock.advance(1)
    assert verifier.grant(session_id) is None


def test_expired_grant_can_be_replaced_by_a_fresh_success() -> None:
    clock = FakeClock()
    verifier = registry(clock)
    session_id = uuid4()
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    clock.advance(VERIFICATION_TTL_SECONDS + 1)
    assert verifier.grant(session_id) is None

    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)
    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER
    clock.advance(VERIFICATION_TTL_SECONDS)
    assert verifier.grant(session_id) is None


def test_record_failure_counts_the_attempt_and_drops_the_grant() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    verifier.record_failure(session_id)

    assert verifier.failures(session_id) == 1
    assert verifier.grant(session_id) is None


def test_five_failures_lock_the_session() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)

    for _ in range(MAX_VERIFICATION_ATTEMPTS):
        assert verifier.locked_out(session_id) is False
        verifier.record_failure(session_id)

    assert verifier.failures(session_id) == MAX_VERIFICATION_ATTEMPTS
    assert verifier.locked_out(session_id) is True
    assert verifier.grant(session_id) is None


def test_success_after_fewer_than_five_failures_clears_the_failure_count() -> None:
    verifier = registry()
    session_id = uuid4()
    for _ in range(MAX_VERIFICATION_ATTEMPTS - 1):
        verifier.record_failure(session_id)

    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    assert verifier.failures(session_id) == 0
    assert verifier.locked_out(session_id) is False
    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER


def test_order_change_clears_the_grant_but_keeps_the_failure_count() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_failure(session_id)
    assert verifier.failures(session_id) == 1

    verifier.set_pending_order(session_id, OTHER_ORDER)

    assert verifier.pending_order(session_id) == OTHER_ORDER
    assert verifier.grant(session_id) is None
    # Failures survive an order change: otherwise five bad attempts could be
    # erased by naming a different order and the lockout would be bypassable.
    assert verifier.failures(session_id) == 1


def test_absent_grant_fields_are_stored_as_none_not_sentinel_values() -> None:
    clock = FakeClock()
    verifier = registry(clock)
    session_id = uuid4()

    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    state = verifier._sessions[session_id]
    assert state.verified_order is None
    assert state.expires_at is None

    verifier.record_failure(session_id)
    assert state.verified_order is None
    assert state.expires_at is None

    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)
    clock.advance(VERIFICATION_TTL_SECONDS)
    assert verifier.grant(session_id) is None
    assert state.verified_order is None
    assert state.expires_at is None

    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)
    verifier.clear_grant(session_id)
    assert state.pending_order is None
    assert state.verified_order is None
    assert state.expires_at is None


def test_grant_requires_both_a_verified_order_and_an_expiry() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    state = verifier._sessions[session_id]
    state.verified_order = FIXTURE_ORDER_NUMBER
    state.expires_at = None

    assert verifier.grant(session_id) is None


def test_clear_on_an_unknown_session_is_a_no_op() -> None:
    verifier = registry()

    verifier.clear(uuid4())

    assert verifier._sessions == {}


def test_setting_the_same_pending_order_preserves_the_grant() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)

    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER


def test_clear_grant_drops_the_pending_order_and_grant_but_keeps_failures() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_failure(session_id)

    verifier.clear_grant(session_id)

    assert verifier.pending_order(session_id) is None
    assert verifier.grant(session_id) is None
    assert verifier.failures(session_id) == 1


def test_clear_grant_on_an_unknown_session_is_a_no_op() -> None:
    verifier = registry()

    verifier.clear_grant(uuid4())

    assert verifier._sessions == {}


def test_clear_resets_every_piece_of_session_state() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    verifier.clear(session_id)

    assert verifier.pending_order(session_id) is None
    assert verifier.grant(session_id) is None
    assert verifier.failures(session_id) == 0
    assert verifier.locked_out(session_id) is False


def test_record_failure_without_prior_state_creates_session_state() -> None:
    verifier = registry()
    session_id = uuid4()

    verifier.record_failure(session_id)

    assert verifier.failures(session_id) == 1
    assert verifier.pending_order(session_id) is None


def test_default_monotonic_clock_is_used_when_no_clock_is_injected() -> None:
    verifier = VerificationRegistry()
    session_id = uuid4()

    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER


def test_tool_request_allows_only_the_single_empty_argument_call() -> None:
    request = ToolRequest(name="lookup_verified_order", arguments={})
    assert request.name == "lookup_verified_order"
    assert request.arguments == {}

    with pytest.raises(ValidationError):
        ToolRequest(
            name="lookup_verified_order", arguments={"order_number": OTHER_ORDER}
        )
    with pytest.raises(ValidationError):
        ToolRequest(name="delete_order", arguments={})
    with pytest.raises(ValidationError):
        ToolRequest(name="lookup_verified_order", arguments={}, extra="x")


def test_extraction_tool_request_is_optional_and_strict() -> None:
    without_tool = extraction()
    assert without_tool.tool_request is None

    with_tool = extraction(
        tool_request=ToolRequest(name="lookup_verified_order", arguments={})
    )
    assert with_tool.tool_request is not None
    assert with_tool.tool_request.name == "lookup_verified_order"

    schema = Extraction.model_json_schema()
    assert "tool_request" in schema["properties"]


def test_verification_request_is_strict_about_shape() -> None:
    command = OrderVerificationRequest(
        delivery_postcode="SW1A 1AA", verification_code="246810"
    )
    assert command.delivery_postcode == "SW1A 1AA"

    # Empty strings are accepted by the wire model and rejected by the Backend API
    # with the same generic error, so shape and credential errors stay distinct.
    assert (
        OrderVerificationRequest(
            delivery_postcode="", verification_code=""
        ).verification_code
        == ""
    )

    with pytest.raises(ValidationError):
        OrderVerificationRequest(
            delivery_postcode="SW1A 1AA", verification_code="246810", extra=True
        )
    with pytest.raises(ValidationError):
        OrderVerificationRequest(delivery_postcode="SW1A 1AA", verification_code=246810)


def test_localized_auth_strings_are_exact_in_english_and_spanish() -> None:
    assert text("auth.demo_code_notice", "en") == (
        "Demo account NS-000123, postcode SW1A 1AA, code 246810. Email delivery is simulated; no email is sent."
    )
    assert text("auth.demo_code_notice", "es") == (
        "Cuenta de demostración NS-000123, código postal SW1A 1AA, código 246810. "
        "El envío de correo es simulado; no se envía ningún correo."
    )
    assert text("auth.verification_failed", "en") == (
        "We couldn't verify those details. Check the postcode and code and try again."
    )
    assert text("auth.verification_failed", "es") == (
        "No pudimos verificar esos datos. Comprueba el código postal y el código "
        "e inténtalo de nuevo."
    )
    assert text("auth.verified", "en") == "Energy account verified for this demo."
    assert (
        text("auth.verified", "es")
        == "Cuenta de energía verificada para esta demostración."
    )
    assert text("auth.verification_prompt", "en") == (
        "Enter the service postcode and demo verification code for this energy account."
    )
    assert text("auth.order_result", "en") == (
        "I found the Northstar Energy account {order_number} for {first_name}: "
        "{item_names}. Account status: {status}."
    )
    assert text("auth.status_active", "en") == "Active"
    assert text("auth.status_active", "es") == "Activa"
    assert text("errors.too_many_verification_attempts", "en")
    assert text("errors.too_many_verification_attempts", "es")
    assert text("errors.backend_api_unavailable", "en") == (
        "The backend service is unavailable. Please retry."
    )
    assert text("errors.backend_api_timeout", "es") == (
        "El servicio interno agotó el tiempo de espera. Inténtalo de nuevo."
    )


def test_clear_pending_closes_the_form_for_an_unverified_order_and_keeps_the_failures() -> (
    None
):
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_failure(session_id)

    verifier.clear_pending(session_id)

    assert verifier.pending_order(session_id) is None
    assert verifier.failures(session_id) == 1


def test_clear_pending_leaves_a_verified_order_alone() -> None:
    verifier = registry()
    session_id = uuid4()
    verifier.set_pending_order(session_id, FIXTURE_ORDER_NUMBER)
    verifier.record_success(session_id, FIXTURE_ORDER_NUMBER)

    verifier.clear_pending(session_id)

    assert verifier.pending_order(session_id) == FIXTURE_ORDER_NUMBER
    assert verifier.grant(session_id) == FIXTURE_ORDER_NUMBER


def test_clear_pending_on_an_unknown_session_is_a_no_op() -> None:
    verifier = registry()

    verifier.clear_pending(uuid4())

    assert verifier._sessions == {}
