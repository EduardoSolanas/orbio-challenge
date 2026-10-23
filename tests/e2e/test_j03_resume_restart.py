"""J03: resume across a failed GET, Retry, and a real server restart (IC §13.3)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import SESSION_ITEM, body_of, wait_until
from tests.support.fakes import (
    ASK_CATEGORY,
    ASK_URGENCY,
    CATEGORY,
    HIGH,
    PART,
    RECEIPT_TEMPLATE,
    summary_text,
    x_category,
    x_high,
    x_part,
)

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


@pytest.mark.parametrize("case_id", [pytest.param("J03", id="J03")])
def test_j03_resume_restart(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())

    # Partial intake.
    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    context_before = ui.transcript_text()
    saved_bytes = server.session_bytes(sid)
    assert ui.disk(sid)["revision"] == 1
    ui.cp("J03.partial-created")

    # Reload hits one controlled pre-handler 503 on the resume GET.
    fault = server.faults.fail_next("GET", rf"/api/sessions/{sid}$", status=503)
    ui.page.reload()
    expect(ui.button("Retry")).to_be_visible()
    expect(ui.alerts.filter(has_text=re.compile(r"\S")).first).to_be_visible()
    assert fault.triggered == 1
    faulted = [g for g in ui.session_gets() if g["faulted"]]
    assert len(faulted) == 1 and faulted[0]["status"] == 503
    assert ui.stored_session_id() == sid
    assert len(ui.create_posts()) == 1, "a failed resume must not POST a replacement session"
    assert server.session_bytes(sid) == saved_bytes
    assert server.session_ids() == [sid]
    ui.cp("J03.retry-visible-no-replacement")

    # Explicit Retry is a GET to the same ID and restores identical context.
    gets_before = len(ui.session_gets())
    with ui.page.expect_response(
        lambda r: r.request.method == "GET" and r.url.endswith(f"/api/sessions/{sid}")
    ) as pending:
        ui.button("Retry").click()
    assert pending.value.status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    expect(ui.button("Retry")).to_be_hidden()
    assert ui.transcript_text() == context_before
    assert len(ui.session_gets()) == gets_before + 1
    assert ui.stored_session_id() == sid
    assert len(ui.create_posts()) == 1
    assert server.session_bytes(sid) == saved_bytes
    assert provider.methods() == ["extract"]
    ui.cp("J03.retry-restores-context")

    # Stop and restart the real server at the same origin and directory.
    origin = server.base_url
    server.restart()
    assert server.base_url == origin
    ui.page.reload()
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.transcript_text() == context_before
    assert ui.stored_session_id() == sid
    assert ui.disk(sid)["revision"] == 1
    assert server.session_bytes(sid) == saved_bytes
    assert len(ui.create_posts()) == 1
    assert provider.methods() == ["extract"], "GET after restart must not call the model"
    ui.cp("J03.restart-preserves-history")

    # Finish through review and confirmation on the restarted server.
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    for line in SUMMARY.split("\n"):
        expect(ui.review).to_contain_text(line)
    saved = ui.disk(sid)
    assert saved["revision"] == 3 and saved["pending_review_revision"] == 3
    assert ui.confirm().status == 200
    expect(ui.log).to_contain_text(RECEIPT_TEMPLATE.format(session_id=sid))
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 4
    assert saved["summary"] == SUMMARY and saved["confirmation"]["review_revision"] == 3
    confirms = ui.confirm_posts()
    assert len(confirms) == 1 and body_of(confirms[0])["review_revision"] == 3
    assert provider.methods() == ["extract", "extract", "extract"]
    assert len(ui.requests("GET", SESSION_ITEM)) >= 4
    wait_until(lambda: len(ui.create_posts()) == 1, "no replacement session at any point")
    provider.assert_consumed()
    ui.cp("J03.finish-after-restart")
