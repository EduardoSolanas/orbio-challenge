"""Stale second tab hits a 409, refreshes, then resolves explicitly (IC §13.3)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import TURNS, body_of, response_of, wait_until
from tests.support.fakes import (
    CATEGORY,
    CHANGE,
    CONFLICT_ORDER,
    HIGH,
    PART,
    USE_NEW,
    conflict_question,
    extraction,
    receipt_for,
    summary_text,
    x_category,
    x_change,
    x_high,
    x_part,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

CONFLICT_ALERT = "This session changed. Refresh before sending again."
HIGH_SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")
FINAL_SUMMARY = summary_text("NS-000999", "damaged_item", "My order arrived broken", "low")
CONFLICT = conflict_question("NS-000123", "NS-000999", "energy account reference")


@pytest.mark.parametrize("case_id", [pytest.param("two_tab_conflict", id="two_tab_conflict")])
def test_two_tab_conflict(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())
    provider.script_extract(CHANGE, x_change())
    provider.script_extract(CONFLICT_ORDER, extraction(order_number="NS-000999"))
    provider.script_extract(
        USE_NEW,
        extraction(
            intent="correction", order_number="NS-000999", explicit_corrections=["order_number"]
        ),
    )

    # Tab A builds a review at revision 3; tab B opens the same session and sees it.
    page_a = ui.page
    ui.open(page_a)
    ui.expect_greeting()
    sid = ui.current_session_id()
    for text in (PART, CATEGORY, HIGH):
        assert ui.send(text).status == 200
    expect(ui.review).to_be_visible()
    assert ui.disk(sid)["pending_review_revision"] == 3

    page_b = ui.new_page()
    ui.open(page_b)
    assert ui.stored_session_id(page_b) == sid
    log_b = page_b.get_by_role("log")
    review_b = page_b.get_by_role("region", name="Review details")
    message_b = page_b.get_by_role("textbox", name="Message")
    expect(review_b).to_contain_text("Urgency: high")
    expect(page_b.get_by_role("button", name="Confirm details", exact=True)).to_be_visible()
    ui.cp("two_tab_conflict.two-tabs-share-review")

    # A commits CHANGE: revision 4, urgency low, fresh review token 4.
    assert ui.send(CHANGE).status == 200
    expect(ui.review).to_contain_text("Urgency: low")
    saved = ui.disk(sid)
    assert saved["revision"] == 4 and saved["pending_review_revision"] == 4
    assert saved["slots"]["urgency"] == "low"
    ui.cp("two_tab_conflict.tab-a-commits-change")

    # B submits from revision 3: visible 409, draft retained, GET refresh only.
    extracts_before = len(provider.calls("extract"))
    assert extracts_before == 4
    assert ui.send(CONFLICT_ORDER, page=page_b).status == 409
    stale = ui.turn_posts()[-1]
    assert stale["status"] == 409
    assert response_of(stale)["error"]["code"] == "revision_conflict"
    assert response_of(stale)["error"]["current_revision"] == 4
    assert body_of(stale)["expected_revision"] == 3
    ui.expect_alert(CONFLICT_ALERT, page_b)
    expect(message_b).to_have_value(CONFLICT_ORDER)
    server.log.wait_until(
        lambda: any(
            g["seq"] > stale["seq"] and g["done"] and g["path"].endswith(sid)
            for g in ui.session_gets()
        ),
        what="GET refresh after the 409",
    )
    expect(log_b).to_contain_text(CHANGE)
    expect(review_b).to_contain_text("Urgency: low")
    assert len(ui.turn_posts()) == 5, "no automatic replay of the stale request"
    assert len(provider.calls("extract")) == extracts_before, "the 409 must not call the model"
    assert ui.disk(sid)["revision"] == 4
    ui.cp("two_tab_conflict.stale-conflict-refresh")

    # B sends explicitly: a new request ID against the current revision.
    with page_b.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/turns")
    ) as pending:
        page_b.get_by_role("button", name="Send", exact=True).click()
    assert pending.value.status == 200
    explicit = ui.turn_posts()[-1]
    assert body_of(explicit)["expected_revision"] == 4
    assert body_of(explicit)["request_id"] != body_of(stale)["request_id"]
    assert body_of(explicit)["text"] == CONFLICT_ORDER
    expect(log_b).to_contain_text(CONFLICT)
    expect(page_b.get_by_role("button", name="Confirm details", exact=True)).to_be_hidden()
    saved = ui.disk(sid)
    assert saved["revision"] == 5 and saved["pending_review_revision"] is None
    assert saved["pending_clarifications"]["order_number"]["code"] == "conflict"
    ui.cp("two_tab_conflict.explicit-send-new-id")

    # B resolves the conflict explicitly and confirms the fresh review.
    assert ui.send(USE_NEW, page=page_b).status == 200
    expect(review_b).to_contain_text("Account reference: NS-000999")
    saved = ui.disk(sid)
    assert saved["revision"] == 6 and saved["pending_review_revision"] == 6
    assert saved["pending_clarifications"] == {} and saved["summary"] == FINAL_SUMMARY
    with page_b.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
    ) as pending:
        page_b.get_by_role("button", name="Confirm details", exact=True).click()
    assert pending.value.status == 200
    receipt = receipt_for(sid)
    expect(log_b).to_contain_text(receipt)
    ui.cp("two_tab_conflict.tab-b-resolves-and-confirms")

    # Only one confirmation; explicitly reload A before comparing its completion.
    confirms = ui.confirm_posts()
    assert len(confirms) == 1 and confirms[0]["status"] == 200
    assert body_of(confirms[0])["expected_revision"] == 6
    assert body_of(confirms[0])["review_revision"] == 6
    page_a.reload()
    expect(page_a.get_by_role("dialog", name="Northstar Energy support chat")).to_be_visible()
    expect(ui.log).to_contain_text(receipt)
    expect(ui.log).to_contain_text("Account reference: NS-000999")
    expect(ui.message).to_be_editable()  # typing stays open after a saved request
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 7
    assert saved["summary"] == FINAL_SUMMARY and saved["confirmation"]["review_revision"] == 6
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert len(server.log.where("POST", TURNS)) == 7
    assert provider.methods() == ["extract"] * 6
    wait_until(lambda: server.session_ids() == [sid], "single session on disk")
    provider.assert_consumed()
    ui.cp("two_tab_conflict.single-confirmation-consistent")
