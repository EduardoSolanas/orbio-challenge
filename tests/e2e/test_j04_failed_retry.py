"""J04: hold a text turn, fail it recoverably, Retry the identical request (IC §13.3)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of
from tests.support.fakes import (
    FULL,
    RECEIPT_TEMPLATE,
    Gate,
    review_reply,
    summary_text,
    x_full,
)

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")
UNAVAILABLE = "The AI service is unavailable. Please retry."


@pytest.mark.parametrize("case_id", [pytest.param("J04", id="J04")])
def test_j04_failed_retry(case_id, ui, server, provider):
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
    for name in ("Send", "Confirm details", "Transcribe audio", "New intake"):
        expect(ui.enabled_button(name)).to_have_count(0)
    posts = ui.turn_posts()
    assert len(posts) == 1 and posts[0]["done"] is False
    assert provider.methods() == ["extract"]
    assert server.session_bytes(sid) == baseline
    ui.cp("J04.pending-locks-one-request")

    # Recoverable failure: draft retained, nothing committed, no fake success.
    gate.release()
    ui.expect_alert(UNAVAILABLE)
    expect(ui.button("Retry")).to_be_visible()
    expect(ui.message).to_have_value(FULL)
    expect(ui.message).to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(1)
    expect(ui.review).to_be_hidden()
    expect(ui.log).not_to_contain_text("Saved locally")
    posts = ui.turn_posts()
    assert [p["status"] for p in posts] == [503]
    assert server.session_bytes(sid) == baseline
    saved = ui.disk(sid)
    assert saved["revision"] == 0 and saved["requests"] == [] and len(saved["messages"]) == 1
    ui.cp("J04.failure-keeps-draft")

    # Retry sends the identical body and request ID, and succeeds once.
    with ui.page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/turns")
    ) as pending:
        ui.button("Retry").click()
    assert pending.value.status == 200
    posts = ui.turn_posts()
    assert [p["status"] for p in posts] == [503, 200]
    assert posts[0]["request_body"] == posts[1]["request_body"]
    assert body_of(posts[0])["request_id"] == body_of(posts[1])["request_id"]
    assert body_of(posts[1])["text"] == FULL and body_of(posts[1])["expected_revision"] == 0
    expect(ui.review).to_be_visible()
    expect(ui.message).to_have_value("")
    saved = ui.disk(sid)
    assert saved["revision"] == 1 and len(saved["messages"]) == 3
    assert saved["pending_review_revision"] == 1 and saved["summary"] == SUMMARY
    assert ui.last_assistant(sid)["text"] == review_reply(SUMMARY)
    assert len(saved["requests"]) == 1
    ui.cp("J04.retry-identical-request")

    # Confirm: one eventual pair, one review, one receipt.
    assert ui.confirm().status == 200
    receipt = RECEIPT_TEMPLATE.format(session_id=sid)
    expect(ui.log).to_contain_text(receipt)
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 2
    assert saved["collection_turn_count"] == 1
    assert len(saved["messages"]) == 5
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert [m["text"] for m in saved["messages"] if m["role"] == "user"].count(FULL) == 1
    assert [c["outcome"] for c in provider.calls("extract")] == [
        "failed:provider_unavailable",
        "ok",
    ]
    assert provider.methods() == ["extract", "extract"]
    assert len(ui.confirm_posts()) == 1
    provider.assert_consumed()
    ui.cp("J04.confirm-single-success")
