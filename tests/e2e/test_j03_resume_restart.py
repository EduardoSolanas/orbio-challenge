"""J03: resume across a failed GET, Retry, and a real server restart (IC §13.3)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import SESSION_ITEM, body_of, wait_until
from tests.support.fakes import (
    ASK_CATEGORY,
    ASK_URGENCY,
    CAPABILITY_Q,
    CATEGORY,
    HIGH,
    PART,
    RECEIPT_TEMPLATE,
    RESUME,
    UNRELATED,
    summary_text,
    x_capability,
    x_category,
    x_high,
    x_part,
    x_start_intake,
    x_unrelated,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


@pytest.mark.parametrize("case_id", [pytest.param("J03", id="J03")])
def test_j03_resume_restart(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(CAPABILITY_Q, x_capability())
    provider.script_extract(UNRELATED, x_unrelated())
    provider.script_extract(RESUME, x_start_intake())
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
    ui.cp("J03.partial-created")

    # Capability and unrelated turns are side branches: neither starts intake or
    # appends another missing-field prompt while clarification is pending.
    assert ui.send(CAPABILITY_Q).status == 200
    expect(ui.log).to_contain_text(
        "I can answer Northstar Shop policy questions or help record an order issue."
    )
    assert ui.last_assistant(sid)["text"] == (
        "I can answer Northstar Shop policy questions or help record an order issue."
    )
    assert ui.disk(sid)["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert ui.send(UNRELATED).status == 200
    expect(ui.log).to_contain_text(
        "I can answer Northstar Shop policy questions or help record an order issue."
    )
    expect(ui.log).not_to_contain_text(ASK_URGENCY)
    context_before = ui.transcript_text()
    saved_bytes = server.session_bytes(sid)
    ui.cp("J03.capability-and-unrelated-do-not-start-intake")

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
    assert provider.methods() == ["extract", "extract", "extract"]
    ui.cp("J03.retry-restores-context")

    # Stop and restart the real server at the same origin and directory.
    origin = server.base_url
    server.restart()
    assert server.base_url == origin
    ui.page.reload()
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.transcript_text() == context_before
    assert ui.stored_session_id() == sid
    assert ui.disk(sid)["revision"] == 3
    assert server.session_bytes(sid) == saved_bytes
    assert len(ui.create_posts()) == 1
    assert provider.methods() == ["extract", "extract", "extract"], "GET after restart must not call the model"
    ui.cp("J03.restart-preserves-history")

    # Explicit resume follows the pending clarification before new intake work.
    assert ui.send(RESUME).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.last_assistant(sid)["text"] == ASK_CATEGORY
    ui.cp("J03.explicit-resume-pending-clarification")

    # Finish through review and confirmation on the restarted server.
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    for line in SUMMARY.split("\n"):
        expect(ui.review).to_contain_text(line)
    saved = ui.disk(sid)
    assert saved["revision"] == 6 and saved["pending_review_revision"] == 6
    assert ui.confirm().status == 200
    expect(ui.log).to_contain_text(RECEIPT_TEMPLATE.format(session_id=sid))
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 7
    assert saved["summary"] == SUMMARY and saved["confirmation"]["review_revision"] == 6
    confirms = ui.confirm_posts()
    assert len(confirms) == 1 and body_of(confirms[0])["review_revision"] == 6
    assert provider.methods() == ["extract", "extract", "extract", "extract", "extract", "extract"]
    assert len(ui.requests("GET", SESSION_ITEM)) >= 4
    wait_until(lambda: len(ui.create_posts()) == 1, "no replacement session at any point")
    provider.assert_consumed()
    ui.cp("J03.finish-after-restart")
