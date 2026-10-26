"""Correct, resolve a conflict, reject and recover a review (IC §13.3)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of
from tests.support.fakes import (
    ASK_CATEGORY,
    ASK_URGENCY,
    BAD_ORDER,
    CATEGORY,
    CHANGE,
    CONFLICT_ORDER,
    FIX_ORDER,
    HIGH,
    INVALID_ORDER,
    KEEP_OLD,
    NO,
    WHAT_TO_CHANGE,
    conflict_question,
    extraction,
    review_reply,
    summary_text,
    x_category,
    x_change,
    x_high,
    x_no,
    x_part,
)

pytestmark = (
    pytest.mark.browser_e2e
)  # controlled-provider browser workflow, never live Docker/provider proof

HIGH_SUMMARY = summary_text(
    "NS-000123", "damaged_item", "My order arrived broken", "high"
)
LOW_SUMMARY = summary_text(
    "NS-000123", "damaged_item", "My order arrived broken", "low"
)
CONFLICT = conflict_question("NS-000123", "NS-000999", "energy account reference")


@pytest.mark.parametrize(
    "case_id", [pytest.param("rejected_review_recovery", id="rejected_review_recovery")]
)
def test_rejected_review_recovery(case_id, ui, server, provider):
    provider.script_extract(BAD_ORDER, x_part("NS-12345"))
    provider.script_extract(FIX_ORDER, extraction(order_number="NS-000123"))
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())
    provider.script_extract(CONFLICT_ORDER, extraction(order_number="NS-000999"))
    provider.script_extract(
        KEEP_OLD,
        extraction(
            intent="correction",
            order_number="NS-000123",
            explicit_corrections=["order_number"],
        ),
    )
    provider.script_extract(NO, x_no())
    provider.script_extract(CHANGE, x_change())

    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()

    # Invalid five-digit order plus a valid problem: the problem survives.
    assert ui.send(BAD_ORDER).status == 200
    expect(ui.log).to_contain_text(INVALID_ORDER)
    saved = ui.disk(sid)
    assert saved["slots"]["order_number"] is None
    assert saved["slots"]["description"] == "My order arrived broken"
    assert saved["pending_clarifications"] == {
        "order_number": {
            "code": "invalid",
            "candidate": "NS-12345",
            "question": INVALID_ORDER,
        }
    }
    assert saved["revision"] == 1 and saved["pending_review_revision"] is None
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    ui.cp("rejected_review_recovery.invalid-order-keeps-problem")

    # Correct the order: issue cleared, next missing field asked.
    assert ui.send(FIX_ORDER).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    saved = ui.disk(sid)
    assert saved["slots"]["order_number"] == "NS-000123"
    assert saved["pending_clarifications"] == {}
    assert saved["slots"]["description"] == "My order arrived broken"
    ui.cp("rejected_review_recovery.order-corrected")

    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    assert saved["revision"] == 4 and saved["pending_review_revision"] == 4
    assert saved["summary"] == HIGH_SUMMARY
    ui.cp("rejected_review_recovery.review-reached")

    # Implicit conflict: different valid order without an explicit correction.
    assert ui.send(CONFLICT_ORDER).status == 200
    expect(ui.log).to_contain_text(CONFLICT)
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    expect(ui.review).to_be_hidden()
    saved = ui.disk(sid)
    assert saved["slots"]["order_number"] == "NS-000123"
    assert saved["pending_clarifications"] == {
        "order_number": {
            "code": "conflict",
            "candidate": "NS-000999",
            "question": CONFLICT,
        }
    }
    assert saved["pending_review_revision"] is None and saved["summary"] is None
    assert saved["status"] == "collecting" and saved["revision"] == 5
    ui.cp("rejected_review_recovery.conflict-hides-confirm")

    # Explicit retention of the old order restores a fresh review.
    assert ui.send(KEEP_OLD).status == 200
    expect(ui.review).to_be_visible()
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    assert saved["pending_clarifications"] == {}
    assert saved["slots"]["order_number"] == "NS-000123"
    assert saved["revision"] == 6 and saved["pending_review_revision"] == 6
    assert saved["summary"] == HIGH_SUMMARY
    ui.cp("rejected_review_recovery.retain-old-restores-review")

    # NO: rejection clears token and summary, nothing completes.
    assert ui.send(NO).status == 200
    expect(ui.log).to_contain_text(WHAT_TO_CHANGE)
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    expect(ui.review).to_be_hidden()
    saved = ui.disk(sid)
    assert saved["review_rejected"] is True and saved["status"] == "collecting"
    assert saved["pending_review_revision"] is None and saved["summary"] is None
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": "high",
    }
    assert saved["revision"] == 7
    ui.cp("rejected_review_recovery.reject-clears-token")

    # CHANGE: valid correction recovers a fresh review but never completes this turn.
    assert ui.send(CHANGE).status == 200
    expect(ui.review).to_be_visible()
    expect(ui.review).to_contain_text("Urgency: low")
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    assert saved["review_rejected"] is False
    assert saved["status"] == "awaiting_confirmation" and saved["confirmation"] is None
    assert saved["revision"] == 8 and saved["pending_review_revision"] == 8
    assert saved["summary"] == LOW_SUMMARY
    assert ui.last_assistant(sid)["text"] == review_reply(LOW_SUMMARY)
    assert ui.confirm_posts() == []
    ui.cp("rejected_review_recovery.recovery-needs-later-confirm")

    # Later confirmation saves the corrected urgency.
    assert ui.confirm().status == 200
    sent = ui.confirm_posts()
    assert len(sent) == 1
    assert body_of(sent[0])["expected_revision"] == 8
    assert body_of(sent[0])["review_revision"] == 8
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 9
    assert saved["slots"]["urgency"] == "low"
    assert saved["summary"] == LOW_SUMMARY
    assert saved["confirmation"]["review_revision"] == 8
    expect(ui.message).to_be_editable()  # typing stays open after a saved request
    assert provider.methods() == ["extract"] * 8
    provider.assert_consumed()
    ui.cp("rejected_review_recovery.later-confirm-saves-low")
