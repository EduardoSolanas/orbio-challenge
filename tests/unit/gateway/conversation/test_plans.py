"""Reply plans and the reply language."""

from __future__ import annotations

from gateway.conversation.plans import (
    TurnPlan,
    choose_reply_language,
    handoff_plan,
    unchanged_outcome,
)
from tests.support.unit_fakes import make_extraction, make_session, make_slots


def test_a_plan_that_keeps_state_asks_for_no_form_and_completes_nothing() -> None:
    plan = TurnPlan.keeping_state(
        make_session(), "reply", "en", frustration_active=False
    )

    assert plan.order_verification_required is False
    assert plan.completed is False
    assert plan.slot_options == ()


def test_a_hand_off_does_not_complete_the_request() -> None:
    assert handoff_plan(make_session(), "en").completed is False


def test_an_unchanged_outcome_accepts_and_clears_nothing() -> None:
    session = make_session(slots=make_slots("NS-000123"))

    result = unchanged_outcome(session)

    assert result.slots == session.slots
    assert (
        result.accepted_fields == frozenset() and result.cleared_fields == frozenset()
    )


def test_the_reply_language_is_the_requested_one_then_a_supported_detection_then_the_session_language() -> (
    None
):
    spanish_session = make_session(reply_language="es")

    assert (
        choose_reply_language(spanish_session, make_extraction(detected_language="en"))
        == "en"
    )
    assert (
        choose_reply_language(make_session(), make_extraction(detected_language="es"))
        == "es"
    )
    assert (
        choose_reply_language(
            spanish_session, make_extraction(detected_language="unsupported")
        )
        == "es"
    )
    assert (
        choose_reply_language(
            spanish_session,
            make_extraction(detected_language="es", requested_language="en"),
        )
        == "en"
    )
