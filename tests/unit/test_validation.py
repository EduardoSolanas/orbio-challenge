"""Unit tests for deterministic field rules (IC §5)."""

import pytest

from app.prompts import (
    MISSING_CATEGORY,
    MISSING_DESCRIPTION,
    MISSING_ORDER,
    MISSING_URGENCY,
    REJECT_PROMPT,
    REVIEW_SUFFIX,
    build_review_reply,
    build_summary,
)
from app.schemas import Clarification, Extraction, Slots
from app.validation import (
    FIELD_ORDER,
    MergeOutcome,
    merge_extraction,
    normalize_candidate,
    select_reply,
    validate_field,
)


def make_slots(
    order_number: str | None = None,
    category: str | None = None,
    description: str | None = None,
    urgency: str | None = None,
) -> Slots:
    return Slots(
        order_number=order_number,
        category=category,
        description=description,
        urgency=urgency,
    )


def make_extraction(
    candidates: dict[str, str | None] | None = None,
    explicit_corrections: list[str] | None = None,
    ambiguous_fields: list[str] | None = None,
    intent: str = "provide_details",
    has_question: bool = False,
) -> Extraction:
    return Extraction(
        intent=intent,
        has_question=has_question,
        candidates=make_slots(**(candidates or {})),
        explicit_corrections=explicit_corrections or [],
        ambiguous_fields=ambiguous_fields or [],
    )


def test_field_order_is_the_contract_order() -> None:
    assert FIELD_ORDER == ("order_number", "category", "description", "urgency")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456", "NS-123456"),
        ("000123", "NS-000123"),
        ("000000", "NS-000000"),
        ("NS-123456", "NS-123456"),
        ("ns-123456", "NS-123456"),
        ("Ns-000001", "NS-000001"),
        ("  NS-000001  ", "NS-000001"),
        ("\t123456\n", "NS-123456"),
        (None, None),
    ],
)
def test_order_number_is_normalized_to_ns_prefix_with_zeros(
    raw: str | None, expected: str | None
) -> None:
    assert normalize_candidate("order_number", raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "12345",
        "1234567",
        "NS-12345",
        "NS-1234567",
        "XNS-123456",
        "NS-123456 extra",
        "order NS-123456",
        "NS-12 3456",
        "١٢٣٤٥٦",
        "１２３４５６",
        "NS-١٢٣٤٥٦",
        "NS-１２３４５６",
        "",
        "NS-",
    ],
)
def test_non_full_match_order_numbers_are_preserved_and_invalid(raw: str) -> None:
    assert normalize_candidate("order_number", raw) == raw
    assert validate_field("order_number", raw) is False


@pytest.mark.parametrize("value", ["NS-123456", "NS-000123", "NS-000000"])
def test_canonical_order_numbers_validate(value: str) -> None:
    assert validate_field("order_number", value) is True


def test_validate_order_number_rejects_bare_digits() -> None:
    # Bare digits are only valid after normalize_candidate; validate sees the
    # canonical form and a bare value is not canonical.
    assert validate_field("order_number", "123456") is False


CATEGORY_ALIASES = [
    ("delivery", "delivery"),
    ("late delivery", "delivery"),
    ("late package", "delivery"),
    ("missing package", "delivery"),
    ("not delivered", "delivery"),
    ("damaged_item", "damaged_item"),
    ("damaged item", "damaged_item"),
    ("damaged", "damaged_item"),
    ("broken item", "damaged_item"),
    ("wrong_item", "wrong_item"),
    ("wrong item", "wrong_item"),
    ("incorrect item", "wrong_item"),
    ("return_refund", "return_refund"),
    ("return", "return_refund"),
    ("refund", "return_refund"),
    ("return or refund", "return_refund"),
    ("other", "other"),
]

URGENCY_ALIASES = [
    ("low", "low"),
    ("low priority", "low"),
    ("not urgent", "low"),
    ("normal", "normal"),
    ("normal priority", "normal"),
    ("medium", "normal"),
    ("high", "high"),
    ("high priority", "high"),
    ("urgent", "high"),
]


@pytest.mark.parametrize(("alias", "canonical"), CATEGORY_ALIASES)
def test_every_category_alias_maps_to_its_canonical_name(
    alias: str, canonical: str
) -> None:
    assert normalize_candidate("category", alias) == canonical
    assert validate_field("category", canonical) is True


@pytest.mark.parametrize(("alias", "canonical"), URGENCY_ALIASES)
def test_every_urgency_alias_maps_to_its_canonical_name(
    alias: str, canonical: str
) -> None:
    assert normalize_candidate("urgency", alias) == canonical
    assert validate_field("urgency", canonical) is True


@pytest.mark.parametrize(("alias", "canonical"), CATEGORY_ALIASES + URGENCY_ALIASES)
def test_alias_matching_collapses_case_and_whitespace(
    alias: str, canonical: str
) -> None:
    messy = f"  {alias.upper().replace(' ', '   ')}\t"
    assert normalize_candidate(
        "category" if canonical in {c for _, c in CATEGORY_ALIASES} else "urgency",
        messy,
    ) == canonical


@pytest.mark.parametrize(
    "raw",
    ["flowers", "broken", "late delivery please", "return_or_refund", "priority"],
)
def test_unknown_category_is_preserved_trimmed_and_invalid(raw: str) -> None:
    assert normalize_candidate("category", raw) == raw
    assert validate_field("category", raw) is False


@pytest.mark.parametrize("raw", ["very high", "asap", "urgent!", "medium priority"])
def test_unknown_urgency_is_preserved_trimmed_and_invalid(raw: str) -> None:
    assert normalize_candidate("urgency", raw) == raw
    assert validate_field("urgency", raw) is False


def test_category_and_urgency_candidates_trim_surrounding_whitespace() -> None:
    assert normalize_candidate("category", "  flowers  ") == "flowers"
    assert normalize_candidate("urgency", "  ASAP  ") == "ASAP"


def test_absent_category_and_urgency_candidates_stay_null() -> None:
    assert normalize_candidate("category", None) is None
    assert normalize_candidate("urgency", None) is None


def test_description_candidate_is_trimmed_but_not_collapsed() -> None:
    assert normalize_candidate("description", "  My order was broken  ") == (
        "My order was broken"
    )
    assert normalize_candidate("description", "Broken    and\nlate") == (
        "Broken    and\nlate"
    )
    assert normalize_candidate("description", None) is None


@pytest.mark.parametrize(
    "value",
    [
        "1234567890",
        "a" * 2000,
        "a" * 1999,
        "My order arrived broken",
        "high priority",
        "normal priority",
    ],
)
def test_valid_description_lengths_and_text_are_accepted(value: str) -> None:
    assert validate_field("description", value) is True


@pytest.mark.parametrize(
    "value",
    ["", "123456789", "a" * 2001, "  123456789  ", "   "],
)
def test_description_outside_ten_to_two_thousand_characters_is_invalid(
    value: str,
) -> None:
    assert validate_field("description", value) is False


@pytest.mark.parametrize(
    "value",
    [
        "delivery",
        "late delivery",
        "Late   Delivery",
        "damaged item",
        "DAMAGED_ITEM",
        "broken item",
        "wrong item",
        "return or refund",
        "RETURN_REFUND",
        "refund",
        "other",
    ],
)
def test_description_equal_to_a_category_alias_is_invalid(value: str) -> None:
    assert validate_field("description", value) is False


def test_first_valid_values_fill_every_empty_slot() -> None:
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction(
            {
                "order_number": "123456",
                "category": "Late Delivery",
                "description": " My order arrived broken ",
                "urgency": "urgent",
            }
        ),
    )
    assert outcome.slots == make_slots(
        "NS-123456", "delivery", "My order arrived broken", "high"
    )
    assert outcome.pending_clarifications == {}
    assert outcome.accepted_fields == frozenset(FIELD_ORDER)
    assert outcome.cleared_fields == frozenset()


def test_equal_valid_candidate_clears_its_pending_issue() -> None:
    pending = {
        "order_number": Clarification(
            code="invalid", candidate="12345", question="ignored"
        )
    }
    outcome = merge_extraction(
        make_slots(order_number="NS-111111"),
        pending,
        make_extraction({"order_number": "ns-111111"}),
    )
    assert outcome.slots.order_number == "NS-111111"
    assert outcome.pending_clarifications == {}
    assert outcome.accepted_fields == frozenset({"order_number"})
    assert outcome.cleared_fields == frozenset({"order_number"})


def test_invalid_candidate_records_issue_without_overwriting_the_slot() -> None:
    outcome = merge_extraction(
        make_slots(order_number="NS-111111"),
        {},
        make_extraction({"order_number": "12345"}),
    )
    assert outcome.slots.order_number == "NS-111111"
    assert outcome.pending_clarifications == {
        "order_number": Clarification(
            code="invalid",
            candidate="12345",
            question="Please use NS- followed by exactly six digits, for example NS-123456.",
        )
    }
    assert outcome.accepted_fields == frozenset()


def test_invalid_category_description_and_urgency_use_their_own_questions() -> None:
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction(
            {
                "category": "flowers",
                "description": "broken",
                "urgency": "asap",
            }
        ),
    )
    assert outcome.pending_clarifications["category"].question == (
        "Please choose delivery, damaged item, wrong item, return/refund, or other."
    )
    assert outcome.pending_clarifications["description"].question == (
        "Please describe the problem using 10 to 2000 characters, rather than only a category label."
    )
    assert outcome.pending_clarifications["urgency"].question == (
        "Please choose low, normal, or high urgency."
    )
    assert outcome.pending_clarifications["category"].candidate == "flowers"
    assert outcome.slots == make_slots()


def test_different_valid_value_records_a_conflict_and_keeps_the_old_value() -> None:
    outcome = merge_extraction(
        make_slots(category="delivery"),
        {},
        make_extraction({"category": "refund"}),
    )
    assert outcome.slots.category == "delivery"
    assert outcome.pending_clarifications == {
        "category": Clarification(
            code="conflict",
            candidate="return_refund",
            question=(
                "You previously gave delivery. Should I keep that or use "
                "return_refund for problem category?"
            ),
        )
    }
    assert outcome.accepted_fields == frozenset()


def test_explicit_correction_replaces_the_accepted_value() -> None:
    outcome = merge_extraction(
        make_slots(category="delivery"),
        {},
        make_extraction({"category": "refund"}, explicit_corrections=["category"]),
    )
    assert outcome.slots.category == "return_refund"
    assert outcome.pending_clarifications == {}
    assert outcome.accepted_fields == frozenset({"category"})


def test_explicit_correction_of_an_empty_slot_is_a_first_fill() -> None:
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction({"urgency": "high"}, explicit_corrections=["urgency"]),
    )
    assert outcome.slots.urgency == "high"
    assert outcome.pending_clarifications == {}
    assert outcome.accepted_fields == frozenset({"urgency"})


def test_explicit_correction_with_an_invalid_candidate_is_an_invalid_issue() -> None:
    outcome = merge_extraction(
        make_slots(urgency="low"),
        {},
        make_extraction({"urgency": "asap"}, explicit_corrections=["urgency"]),
    )
    assert outcome.slots.urgency == "low"
    assert outcome.pending_clarifications["urgency"].code == "invalid"
    assert outcome.accepted_fields == frozenset()


def test_explicit_retention_of_the_old_value_counts_as_accepted() -> None:
    outcome = merge_extraction(
        make_slots(urgency="low"),
        {},
        make_extraction({"urgency": "low"}, explicit_corrections=["urgency"]),
    )
    assert outcome.slots.urgency == "low"
    assert outcome.accepted_fields == frozenset({"urgency"})
    assert outcome.pending_clarifications == {}


def test_ambiguous_flag_wins_over_a_valid_candidate() -> None:
    outcome = merge_extraction(
        make_slots(urgency="low"),
        {},
        make_extraction({"urgency": "high"}, ambiguous_fields=["urgency"]),
    )
    assert outcome.slots.urgency == "low"
    assert outcome.pending_clarifications == {
        "urgency": Clarification(
            code="ambiguous",
            candidate="high",
            question="Please clarify your urgency.",
        )
    }
    assert outcome.accepted_fields == frozenset()


def test_ambiguous_flag_with_null_candidate_still_records_an_issue() -> None:
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction({}, ambiguous_fields=["order_number"]),
    )
    assert outcome.slots == make_slots()
    assert outcome.pending_clarifications == {
        "order_number": Clarification(
            code="ambiguous",
            candidate=None,
            question="Please clarify your order number.",
        )
    }


def test_ambiguous_flag_wins_over_an_explicit_correction() -> None:
    outcome = merge_extraction(
        make_slots(category="delivery"),
        {},
        make_extraction(
            {"category": "refund"},
            explicit_corrections=["category"],
            ambiguous_fields=["category"],
        ),
    )
    assert outcome.slots.category == "delivery"
    assert outcome.pending_clarifications["category"].code == "ambiguous"


def test_null_candidate_never_erases_an_accepted_slot() -> None:
    outcome = merge_extraction(
        make_slots(description="My order arrived broken"),
        {},
        make_extraction({"description": None}),
    )
    assert outcome.slots.description == "My order arrived broken"
    assert outcome.pending_clarifications == {}
    assert outcome.accepted_fields == frozenset()


def test_null_candidate_does_not_clear_an_existing_issue() -> None:
    pending = {
        "description": Clarification(
            code="invalid", candidate="broken", question="ignored"
        )
    }
    outcome = merge_extraction(
        make_slots(),
        pending,
        make_extraction({"description": None}),
    )
    assert outcome.pending_clarifications == pending
    assert outcome.cleared_fields == frozenset()


def test_unrelated_topic_never_drops_a_pending_issue() -> None:
    pending = {
        "order_number": Clarification(
            code="invalid", candidate="12345", question="ignored"
        )
    }
    outcome = merge_extraction(
        make_slots(order_number="NS-111111"),
        pending,
        make_extraction(intent="unrelated"),
    )
    assert outcome.pending_clarifications == pending
    assert outcome.slots.order_number == "NS-111111"
    assert outcome.accepted_fields == frozenset()


def test_merge_is_independent_per_field() -> None:
    outcome = merge_extraction(
        make_slots(order_number="NS-111111", category="delivery"),
        {},
        make_extraction(
            {
                "order_number": "ns-111111",
                "category": "refund",
                "description": "My order arrived broken",
                "urgency": "high",
            },
            explicit_corrections=["category"],
        ),
    )
    assert outcome.slots == make_slots(
        "NS-111111", "return_refund", "My order arrived broken", "high"
    )
    assert outcome.pending_clarifications == {}
    assert outcome.accepted_fields == frozenset(
        {"order_number", "category", "description", "urgency"}
    )


def test_pending_issues_are_ordered_by_the_contract_field_order() -> None:
    pending = {
        "description": Clarification(
            code="invalid", candidate="broken", question="ignored"
        )
    }
    outcome = merge_extraction(
        make_slots(),
        pending,
        make_extraction({"order_number": "12345"}),
    )
    assert list(outcome.pending_clarifications) == ["order_number", "description"]


def test_a_new_conflict_replaces_the_pending_issue_for_that_field() -> None:
    pending = {
        "category": Clarification(
            code="invalid", candidate="flowers", question="ignored"
        )
    }
    outcome = merge_extraction(
        make_slots(category="delivery"),
        pending,
        make_extraction({"category": "refund"}),
    )
    assert outcome.pending_clarifications["category"].code == "conflict"
    assert outcome.cleared_fields == frozenset()


def test_ambiguous_order_number_candidate_is_still_normalized() -> None:
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction({"order_number": "123456"}, ambiguous_fields=["order_number"]),
    )
    assert outcome.pending_clarifications["order_number"].candidate == "NS-123456"


def test_ambiguous_field_does_not_stop_merging_later_fields() -> None:
    outcome = merge_extraction(
        make_slots(),
        {},
        make_extraction({"category": "flowers"}, ambiguous_fields=["order_number"]),
    )
    assert list(outcome.pending_clarifications) == ["order_number", "category"]


def test_explicit_correction_clears_its_existing_pending_issue() -> None:
    pending = {
        "category": Clarification(
            code="conflict", candidate="return_refund", question="ignored"
        )
    }
    outcome = merge_extraction(
        make_slots(category="delivery"),
        pending,
        make_extraction({"category": "wrong item"}, explicit_corrections=["category"]),
    )
    assert outcome.slots.category == "wrong_item"
    assert outcome.pending_clarifications == {}
    assert outcome.cleared_fields == frozenset({"category"})


def test_merge_returns_a_new_slots_object_and_leaves_the_input_untouched() -> None:
    original = make_slots()
    outcome = merge_extraction(
        original,
        {},
        make_extraction({"order_number": "123456"}),
    )
    assert isinstance(outcome, MergeOutcome)
    assert outcome.slots is not original
    assert original.order_number is None


def test_new_reject_wins_over_every_pending_issue() -> None:
    pending = {
        "order_number": Clarification(
            code="invalid", candidate="12345", question="ignored"
        )
    }
    reply = select_reply(
        new_reject=True,
        pending_clarifications=pending,
        review_rejected=True,
        slots=make_slots(),
    )
    assert reply == REJECT_PROMPT == "What would you like to change?"


def test_first_pending_issue_is_asked_in_field_order() -> None:
    pending = {
        "urgency": Clarification(code="invalid", candidate="asap", question="U?"),
        "description": Clarification(
            code="invalid", candidate="broken", question="D?"
        ),
        "order_number": Clarification(
            code="invalid", candidate="12345", question="O?"
        ),
    }
    assert (
        select_reply(
            new_reject=False,
            pending_clarifications=pending,
            review_rejected=False,
            slots=make_slots(),
        )
        == "O?"
    )
    assert (
        select_reply(
            new_reject=False,
            pending_clarifications={
                "urgency": pending["urgency"],
                "description": pending["description"],
            },
            review_rejected=False,
            slots=make_slots(),
        )
        == "D?"
    )


def test_pending_issue_wins_over_the_rejection_prompt() -> None:
    pending = {
        "category": Clarification(code="conflict", candidate="refund", question="C?")
    }
    assert (
        select_reply(
            new_reject=False,
            pending_clarifications=pending,
            review_rejected=True,
            slots=make_slots(category="delivery"),
        )
        == "C?"
    )


def test_review_rejected_without_pending_issues_asks_for_a_change() -> None:
    assert (
        select_reply(
            new_reject=False,
            pending_clarifications={},
            review_rejected=True,
            slots=make_slots(),
        )
        == "What would you like to change?"
    )


@pytest.mark.parametrize(
    ("slots", "expected"),
    [
        (make_slots(), MISSING_ORDER),
        (make_slots(order_number="NS-123456"), MISSING_CATEGORY),
        (
            make_slots(order_number="NS-123456", category="delivery"),
            MISSING_DESCRIPTION,
        ),
        (
            make_slots(
                order_number="NS-123456",
                category="delivery",
                description="My order arrived broken",
            ),
            MISSING_URGENCY,
        ),
    ],
)
def test_first_missing_field_question_is_asked(slots: Slots, expected: str) -> None:
    assert (
        select_reply(
            new_reject=False,
            pending_clarifications={},
            review_rejected=False,
            slots=slots,
        )
        == expected
    )


def test_complete_slots_select_the_review_reply() -> None:
    slots = make_slots(
        "NS-000123", "damaged_item", "My order arrived broken", "high"
    )
    assert (
        select_reply(
            new_reject=False,
            pending_clarifications={},
            review_rejected=False,
            slots=slots,
        )
        == build_review_reply(slots)
    )


def test_summary_uses_canonical_values_and_contract_format() -> None:
    slots = make_slots(
        "NS-000123", "damaged_item", "My order arrived broken", "high"
    )
    assert build_summary(slots) == (
        "Order: NS-000123\n"
        "Category: damaged_item\n"
        "Problem: My order arrived broken\n"
        "Urgency: high"
    )


def test_review_reply_is_the_summary_plus_the_review_suffix() -> None:
    slots = make_slots(
        "NS-000123", "damaged_item", "My order arrived broken", "high"
    )
    assert build_review_reply(slots) == build_summary(slots) + REVIEW_SUFFIX
    assert REVIEW_SUFFIX == (
        "\nPlease confirm these details or tell me what to change."
    )


@pytest.mark.parametrize(
    "slots",
    [
        make_slots(),
        make_slots(order_number="NS-123456"),
        make_slots(order_number="NS-123456", category="delivery"),
        make_slots(
            order_number="NS-123456",
            category="delivery",
            description="My order arrived broken",
        ),
    ],
)
def test_summary_is_never_built_with_a_missing_field(slots: Slots) -> None:
    with pytest.raises(ValueError):
        build_summary(slots)
