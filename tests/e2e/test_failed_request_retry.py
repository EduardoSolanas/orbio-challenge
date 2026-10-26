"""Hold a text turn, fail it recoverably, Retry the identical request (IC §13.3)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of
from tests.support.fakes import (
    FULL,
    Gate,
    receipt_for,
    review_reply,
    summary_text,
    x_full,
)

pytestmark = (
    pytest.mark.browser_e2e
)  # controlled-provider browser workflow, never live Docker/provider proof

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")
UNAVAILABLE = "The AI service is unavailable. Please retry."


@pytest.mark.parametrize(
    "case_id", [pytest.param("failed_request_retry", id="failed_request_retry")]
)
def test_failed_request_retry(case_id, ui, server, provider):
    gate = Gate("FULL extraction")
    provider.script_extract(FULL, fail="provider_unavailable", hold=gate)
    provider.script_extract(FULL, x_full())

    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    baseline = server.session_bytes(sid)

    # One request despite a rapid second click; controls locked while pending.
    ui.message.fill(FULL)
    ui.button("Send").dblclick()
    gate.wait_entered()
    expect(ui.message).to_have_js_property("readOnly", True)
    expect(ui.message).to_have_value("")
    expect(ui.log).to_contain_text(FULL)
    thinking = ui.log.get_by_role("status", name="Northstar is thinking")
    expect(thinking).to_be_visible()
    expect(thinking.locator(".dots")).to_be_visible()
    for name in ("Send", "Confirm details", "New conversation"):
        expect(ui.enabled_button(name)).to_have_count(0)
    expect(ui.file_input).to_be_disabled()
    posts = ui.turn_posts()
    assert len(posts) == 1 and posts[0]["done"] is False
    assert provider.methods() == ["extract"]
    assert server.session_bytes(sid) == baseline
    ui.cp("failed_request_retry.pending-locks-one-request")

    # Recoverable failure: draft retained, nothing committed, no fake success.
    gate.release()
    ui.expect_alert(UNAVAILABLE)
    expect(ui.button("Retry")).to_be_hidden()
    expect(ui.log.get_by_role("status", name="Northstar is thinking")).to_have_count(0)
    expect(ui.message).to_have_value(FULL)
    expect(ui.message).to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(1)
    expect(ui.review).to_be_hidden()
    expect(ui.log).not_to_contain_text("Saved locally")
    posts = ui.turn_posts()
    assert [p["status"] for p in posts] == [503]
    assert server.session_bytes(sid) == baseline
    saved = ui.disk(sid)
    assert (
        saved["revision"] == 0
        and saved["requests"] == []
        and len(saved["messages"]) == 1
    )
    ui.cp("failed_request_retry.failure-keeps-draft")

    # Resending the restored draft creates a fresh request and succeeds once.
    with ui.page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/turns")
    ) as pending:
        ui.button("Send").click()
    assert pending.value.status == 200
    posts = ui.turn_posts()
    assert [p["status"] for p in posts] == [503, 200]
    assert posts[0]["request_body"] != posts[1]["request_body"]
    assert body_of(posts[0])["request_id"] != body_of(posts[1])["request_id"]
    assert (
        body_of(posts[1])["text"] == FULL
        and body_of(posts[1])["expected_revision"] == 0
    )
    expect(ui.review).to_be_visible()
    expect(ui.message).to_have_value("")
    saved = ui.disk(sid)
    assert saved["revision"] == 1 and len(saved["messages"]) == 3
    assert saved["pending_review_revision"] == 1 and saved["summary"] == SUMMARY
    assert ui.last_assistant(sid)["text"] == review_reply(SUMMARY)
    assert len(saved["requests"]) == 1
    ui.cp("failed_request_retry.retry-identical-request")

    # Confirm: one eventual pair, one review, one receipt.
    assert ui.confirm().status == 200
    receipt = receipt_for(sid)
    expect(ui.log).to_contain_text(receipt)
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 2
    assert saved["collection_turn_count"] == 1
    assert len(saved["messages"]) == 5
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert [m["text"] for m in saved["messages"] if m["role"] == "user"].count(
        FULL
    ) == 1
    assert [c["outcome"] for c in provider.calls("extract")] == [
        "failed:provider_unavailable",
        "ok",
    ]
    assert provider.methods() == ["extract", "extract"]
    assert len(ui.confirm_posts()) == 1
    provider.assert_consumed()
    ui.cp("failed_request_retry.confirm-single-success")
