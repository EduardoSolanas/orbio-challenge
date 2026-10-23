"""IT03-01..IT03-12: field validation through the real HTTP turn route.

Business-invalid inputs stay conversational 200s with the exact clarification,
valid siblings and previously accepted values are retained, and issues clear
only through explicit valid answers (IC §5, §13.2 IT03). Fixtures are scripted
extractions at the AI boundary only; the app, engine and JSON repository are real.
"""

from __future__ import annotations

import pytest

from tests.integration.support_api import make_extraction, read_session
from tests.integration.test_it02_intake import (
    EMPTY_SLOTS,
    FULL_SLOTS,
    FULL_SUMMARY,
    FULL_TEXT,
    INVALID_ORDER,
    REVIEW_SUFFIX,
    assert_issue,
    assert_normal_turn,
    full_extraction,
    part_extraction,
    post_turn,
    start_session,
)

MISSING_ORDER = (
    "What is your order number? Use NS- followed by six digits, for example NS-123456."
)
MISSING_URGENCY = "How urgent is this: low, normal, or high?"
INVALID_CATEGORY = (
    "Please choose delivery, damaged item, wrong item, return/refund, or other."
)
INVALID_DESCRIPTION = (
    "Please describe the problem using 10 to 2000 characters, rather than only a category label."
)
INVALID_URGENCY = "Please choose low, normal, or high urgency."

NINE_CHARACTER_DESCRIPTION = "Broken it"
TEN_CHARACTER_DESCRIPTION = "It broke!!"
TWO_THOUSAND_CHARACTER_DESCRIPTION = ("My order was damaged. " * 100)[:2000]
TWO_THOUSAND_ONE_CHARACTER_DESCRIPTION = "b" * 2001
FULL_WIDTH_ORDER = "NS-\uff11\uff12\uff13\uff14\uff15\uff16"


async def _disk(session_dir, session_id):
    return read_session(session_dir, session_id)


async def case_it03_01(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "My order NS-12345 arrived broken."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={
                "order_number": "NS-12345",
                "description": "My order arrived broken",
            }
        ),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "description": "My order arrived broken",
    }
    assert_issue(response, "order_number", "invalid", "NS-12345", INVALID_ORDER)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_02(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "My order NS-1234567 arrived broken."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(candidates={"order_number": "NS-1234567"}),
    )
    assert response["slots"] == EMPTY_SLOTS
    assert_issue(response, "order_number", "invalid", "NS-1234567", INVALID_ORDER)
    assert response["status"] == "collecting"
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_03(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = f"My order {FULL_WIDTH_ORDER} arrived broken."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(candidates={"order_number": FULL_WIDTH_ORDER}),
    )
    assert response["slots"] == EMPTY_SLOTS
    assert_issue(
        response, "order_number", "invalid", FULL_WIDTH_ORDER, INVALID_ORDER
    )
    assert response["pending_clarifications"]["order_number"]["candidate"] == (
        FULL_WIDTH_ORDER
    )
    assert response["status"] == "collecting"
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_04(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "It is a banana."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(candidates={"category": "banana"}),
    )
    assert response["slots"] == EMPTY_SLOTS
    assert response["slots"]["category"] is None
    assert_issue(response, "category", "invalid", "banana", INVALID_CATEGORY)
    assert response["status"] == "collecting"
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_05(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "It broke."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={"description": NINE_CHARACTER_DESCRIPTION}
        ),
    )
    assert len(NINE_CHARACTER_DESCRIPTION) == 9
    assert response["slots"] == EMPTY_SLOTS
    assert_issue(
        response,
        "description",
        "invalid",
        NINE_CHARACTER_DESCRIPTION,
        INVALID_DESCRIPTION,
    )
    assert response["status"] == "collecting"
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_06(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "It broke!!"
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={"description": TEN_CHARACTER_DESCRIPTION}
        ),
    )
    assert len(TEN_CHARACTER_DESCRIPTION) == 10
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "description": TEN_CHARACTER_DESCRIPTION,
    }
    assert response["pending_clarifications"] == {}
    assert response["reply"] == MISSING_ORDER
    assert response["status"] == "collecting"
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_07(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "My order description is long."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={"description": TWO_THOUSAND_CHARACTER_DESCRIPTION}
        ),
    )
    assert len(TWO_THOUSAND_CHARACTER_DESCRIPTION) == 2000
    assert response["slots"]["description"] == TWO_THOUSAND_CHARACTER_DESCRIPTION
    assert len(response["slots"]["description"]) == 2000
    assert response["pending_clarifications"] == {}
    assert response["reply"] == MISSING_ORDER
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_08(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text="My order NS-000123 arrived broken.",
        revision=0,
        extraction=part_extraction(),
    )
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text="My order NS-000123 arrived broken.",
    )

    text = "It was a long story."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(
            candidates={"description": TWO_THOUSAND_ONE_CHARACTER_DESCRIPTION}
        ),
    )
    assert len(TWO_THOUSAND_ONE_CHARACTER_DESCRIPTION) == 2001
    assert response["slots"]["description"] == "My order arrived broken"
    assert_issue(
        response,
        "description",
        "invalid",
        TWO_THOUSAND_ONE_CHARACTER_DESCRIPTION,
        INVALID_DESCRIPTION,
    )
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_09(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "It is a damaged item."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={"category": "damaged_item", "description": "damaged item"}
        ),
    )
    assert response["slots"] == {**EMPTY_SLOTS, "category": "damaged_item"}
    assert response["slots"]["description"] is None
    assert_issue(
        response, "description", "invalid", "damaged item", INVALID_DESCRIPTION
    )
    assert response["status"] == "collecting"
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_10(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "My order NS-000123 arrived broken. It is a damaged item."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={
                "order_number": "NS-000123",
                "category": "damaged_item",
                "description": "My order arrived broken",
            }
        ),
    )
    assert response["slots"] == {**FULL_SLOTS, "urgency": None}
    assert response["slots"]["urgency"] is None
    assert response["pending_clarifications"] == {}
    assert response["reply"] == MISSING_URGENCY
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_11(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=FULL_TEXT,
        revision=0,
        extraction=full_extraction(),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["pending_review_revision"] == 1
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=FULL_TEXT,
    )

    text = "Purple urgency."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(candidates={"urgency": "purple"}),
    )
    assert response["slots"]["urgency"] == "high"
    assert_issue(response, "urgency", "invalid", "purple", INVALID_URGENCY)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    assert response["revision"] == 2
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )


async def case_it03_12(client, provider, session_dir):
    session_id = await start_session(client)
    before = await _disk(session_dir, session_id)
    text = "My order NS-12345 arrived broken and it is purple urgency."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=0,
        extraction=make_extraction(
            candidates={
                "order_number": "NS-12345",
                "category": "damaged_item",
                "description": "My order arrived broken",
                "urgency": "purple",
            }
        ),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "category": "damaged_item",
        "description": "My order arrived broken",
    }
    assert response["pending_clarifications"] == {
        "order_number": {
            "code": "invalid",
            "candidate": "NS-12345",
            "question": INVALID_ORDER,
        },
        "urgency": {
            "code": "invalid",
            "candidate": "purple",
            "question": INVALID_URGENCY,
        },
    }
    assert response["reply"] == INVALID_ORDER
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=0,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )

    text = "My order is NS-000123."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=1,
        extraction=make_extraction(
            intent="correction",
            candidates={"order_number": "NS-000123"},
            explicit_corrections=["order_number"],
        ),
    )
    assert response["slots"] == {
        **EMPTY_SLOTS,
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
    }
    assert_issue(response, "urgency", "invalid", "purple", INVALID_URGENCY)
    assert response["status"] == "collecting"
    assert response["summary"] is None
    assert response["pending_review_revision"] is None
    before = assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=1,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )

    text = "High urgency."
    request_id, response = await post_turn(
        client,
        provider,
        session_id,
        text=text,
        revision=2,
        extraction=make_extraction(
            intent="correction",
            candidates={"urgency": "high"},
            explicit_corrections=["urgency"],
        ),
    )
    assert response["slots"] == FULL_SLOTS
    assert response["pending_clarifications"] == {}
    assert response["status"] == "awaiting_confirmation"
    assert response["pending_review_revision"] == 3
    assert response["summary"] == FULL_SUMMARY
    assert response["reply"] == FULL_SUMMARY + REVIEW_SUFFIX
    assert_normal_turn(
        session_dir=session_dir,
        session_id=session_id,
        provider=provider,
        calls_before=2,
        before_disk=before,
        request_id=request_id,
        response=response,
        text=text,
    )
    assert provider.methods() == ["extract", "extract", "extract"]


CASES = [
    pytest.param(case_it03_01, id="IT03-01"),
    pytest.param(case_it03_02, id="IT03-02"),
    pytest.param(case_it03_03, id="IT03-03"),
    pytest.param(case_it03_04, id="IT03-04"),
    pytest.param(case_it03_05, id="IT03-05"),
    pytest.param(case_it03_06, id="IT03-06"),
    pytest.param(case_it03_07, id="IT03-07"),
    pytest.param(case_it03_08, id="IT03-08"),
    pytest.param(case_it03_09, id="IT03-09"),
    pytest.param(case_it03_10, id="IT03-10"),
    pytest.param(case_it03_11, id="IT03-11"),
    pytest.param(case_it03_12, id="IT03-12"),
]


@pytest.mark.parametrize("case", CASES)
async def test_it03_field_validation(case, client, provider, session_dir):
    await case(client, provider, session_dir)
