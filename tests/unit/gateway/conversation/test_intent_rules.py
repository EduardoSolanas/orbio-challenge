"""The small rules that decide whether a turn confirms, rejects or only changes language."""

from __future__ import annotations

import pytest

from gateway.conversation.intent import (
    apply_transitions,
    is_language_only,
    is_pure_confirmation,
)
from gateway.conversation.validation import MergeOutcome
from gateway.schemas import Clarification
from tests.support.unit_fakes import make_extraction, make_session, make_slots

FULL = make_slots("NS-000123", "damaged_item", "My order arrived broken", "high")


def outcome(slots=FULL, pending=None, accepted=()) -> MergeOutcome:
    return MergeOutcome(
        slots=slots,
        pending_clarifications=pending or {},
        accepted_fields=frozenset(accepted),
        cleared_fields=frozenset(),
    )


def under_review(**changes):
    return make_session(slots=FULL, pending_review_revision=3, **changes)


# --- a language request on its own ----------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"intent": "confirm"}, True),
        ({"intent": "reject"}, False),
        ({"intent": "confirm", "has_question": True}, False),
        ({"intent": "confirm", "candidates": {"urgency": "low"}}, False),
        ({"intent": "confirm", "explicit_corrections": ["urgency"]}, False),
        ({"intent": "confirm", "requested_language": None}, False),
    ],
)
def test_a_language_request_is_only_a_language_request_without_facts_questions_or_a_rejection(
    changes, expected
) -> None:
    extraction = make_extraction(**{"requested_language": "es", **changes})

    assert is_language_only(extraction) is expected


# --- a plain "yes" ---------------------------------------------------------------------


def confirmed(extraction=None, *, session=None, result=None, **flags) -> bool:
    return is_pure_confirmation(
        session or under_review(),
        extraction or make_extraction(intent="confirm"),
        result or outcome(),
        **{
            "review_rejected": False,
            "recovered_this_turn": False,
            "current_review_valid": True,
            **flags,
        },
    )


def test_a_plain_yes_to_a_valid_review_confirms() -> None:
    assert confirmed() is True


@pytest.mark.parametrize(
    "flag",
    [
        {"review_rejected": True},
        {"recovered_this_turn": True},
        {"current_review_valid": False},
    ],
)
def test_each_review_state_alone_stops_a_yes_from_confirming(flag) -> None:
    assert confirmed(**flag) is False


@pytest.mark.parametrize(
    "extraction",
    [
        make_extraction(intent="provide_details"),
        make_extraction(intent="confirm", has_question=True),
        make_extraction(intent="confirm", explicit_corrections=["urgency"]),
        make_extraction(intent="confirm", ambiguous_fields=["category"]),
        make_extraction(intent="confirm", candidates={"urgency": "low"}),
    ],
    ids=[
        "not_a_confirmation",
        "question",
        "correction",
        "ambiguity",
        "different_value",
    ],
)
def test_anything_beyond_a_plain_yes_does_not_confirm(extraction) -> None:
    assert confirmed(extraction) is False


def test_a_yes_that_repeats_the_accepted_values_still_confirms() -> None:
    repeated = make_extraction(
        intent="confirm", candidates={"urgency": "high", "order_number": "NS-000123"}
    )

    assert confirmed(repeated) is True


def test_a_yes_does_not_confirm_while_an_issue_is_pending_or_the_slots_changed() -> (
    None
):
    issue = {"urgency": Clarification(code="invalid", candidate="asap", question="U?")}

    assert confirmed(result=outcome(pending=issue)) is False
    assert (
        confirmed(
            result=outcome(
                slots=make_slots(
                    "NS-000123", "damaged_item", "My order arrived broken", "low"
                )
            )
        )
        is False
    )


# --- what a turn does to a rejected review ---------------------------------------------


def transition(extraction, *, session=None, result=None):
    return apply_transitions(session or under_review(), extraction, result or outcome())


def test_rejecting_a_review_marks_it_rejected() -> None:
    assert transition(make_extraction(intent="reject")) == (True, False)


def test_rejecting_when_there_is_no_review_changes_nothing() -> None:
    assert transition(make_extraction(intent="reject"), session=make_session()) == (
        False,
        False,
    )


def test_a_review_that_was_rejected_stays_rejected_when_rejected_again() -> None:
    assert transition(
        make_extraction(intent="reject"), session=make_session(review_rejected=True)
    ) == (True, False)


CORRECTION = make_extraction(
    intent="correction", candidates={"urgency": "low"}, explicit_corrections=["urgency"]
)


def test_an_accepted_correction_recovers_a_rejected_review() -> None:
    rejected = under_review(review_rejected=True)

    assert transition(
        CORRECTION, session=rejected, result=outcome(accepted={"urgency"})
    ) == (False, True)


def test_a_correction_with_a_question_does_not_recover_it() -> None:
    asking = CORRECTION.model_copy(update={"has_question": True})

    assert transition(
        asking,
        session=under_review(review_rejected=True),
        result=outcome(accepted={"urgency"}),
    ) == (True, False)


def test_an_unambiguous_yes_recovers_a_rejected_review() -> None:
    assert transition(
        make_extraction(intent="confirm"), session=under_review(review_rejected=True)
    ) == (False, True)


def test_unrelated_talk_leaves_a_rejected_review_rejected_and_a_clean_one_clean() -> (
    None
):
    chatter = make_extraction(intent="social")

    assert transition(chatter, session=under_review(review_rejected=True)) == (
        True,
        False,
    )
    assert transition(chatter) == (False, False)
