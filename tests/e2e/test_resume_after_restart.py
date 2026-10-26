"""Resume across a failed GET, Retry, and a real server restart (IC §13.3)."""

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
    GREETING_STANDING_CHARGE_Q,
    HIGH,
    PART,
    RESUME,
    STANDING_CHARGE_ANSWER_TEXT,
    UNRELATED,
    receipt_for,
    standing_charge_answer,
    summary_text,
    x_capability,
    x_category,
    x_high,
    x_part,
    x_question,
    x_start_intake,
    x_unrelated,
)

pytestmark = (
    pytest.mark.browser_e2e
)  # controlled-provider browser workflow, never live Docker/provider proof

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


@pytest.mark.parametrize(
    "case_id", [pytest.param("resume_after_restart", id="resume_after_restart")]
)
def test_resume_after_restart(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(CAPABILITY_Q, x_capability())
    provider.script_extract(UNRELATED, x_unrelated())

    # Partial intake.
    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    context_before = ui.transcript_text()
    saved_bytes = server.session_bytes(sid)
    ui.cp("resume_after_restart.partial-created")

    # Capability and unrelated turns are side branches: neither starts intake or
    # appends another missing-field prompt while clarification is pending.
    assert ui.send(CAPABILITY_Q).status == 200
    expect(ui.log).to_contain_text(
        "I can answer questions about Northstar Energy bills, tariffs, meter readings, moving home, and supply issues. I can also help you report a service problem and, once you verify the demo account, check an installation or book a technician. What do you need help with?"
    )
    assert ui.last_assistant(sid)["text"] == (
        "I can answer questions about Northstar Energy bills, tariffs, meter readings, moving home, and supply issues. I can also help you report a service problem and, once you verify the demo account, check an installation or book a technician. What do you need help with?"
    )
    assert ui.disk(sid)["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert ui.send(UNRELATED).status == 200
    expect(ui.log).to_contain_text(
        "I can answer questions about Northstar Energy bills, tariffs, meter readings, moving home, and supply issues. I can also help you report a service problem and, once you verify the demo account, check an installation or book a technician. What do you need help with?"
    )
    expect(ui.log).not_to_contain_text(ASK_URGENCY)
    context_before = ui.transcript_text()
    saved_bytes = server.session_bytes(sid)
    ui.cp("resume_after_restart.capability-and-unrelated-do-not-start-intake")

    # Reload hits one controlled pre-handler 503 on the resume GET.
    fault = server.faults.fail_next("GET", rf"/api/sessions/{sid}$", status=503)
    ui.page.reload()
    expect(
        ui.page.get_by_role("dialog", name="Northstar Energy support chat")
    ).to_be_visible()
    expect(ui.button("Retry")).to_be_visible()
    expect(ui.alerts.filter(has_text=re.compile(r"\S")).first).to_be_visible()
    assert fault.triggered == 1
    faulted = [g for g in ui.session_gets() if g["faulted"]]
    assert len(faulted) == 1 and faulted[0]["status"] == 503
    assert ui.stored_session_id() == sid
    assert len(ui.create_posts()) == 1, (
        "a failed resume must not POST a replacement session"
    )
    assert server.session_bytes(sid) == saved_bytes
    assert server.session_ids() == [sid]
    ui.cp("resume_after_restart.retry-visible-no-replacement")

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
    ui.cp("resume_after_restart.retry-restores-context")

    # Stop and restart the real server at the same origin and directory.
    origin = server.base_url
    server.restart()
    assert server.base_url == origin
    ui.page.reload()
    expect(
        ui.page.get_by_role("dialog", name="Northstar Energy support chat")
    ).to_be_visible()
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.transcript_text() == context_before
    assert ui.stored_session_id() == sid
    assert ui.disk(sid)["revision"] == 3
    assert server.session_bytes(sid) == saved_bytes
    assert len(ui.create_posts()) == 1
    assert provider.methods() == ["extract", "extract", "extract"], (
        "GET after restart must not call the model"
    )
    ui.cp("resume_after_restart.restart-preserves-history")

    # A FAQ after session restoration answers without replacing the pending
    # clarification; explicit resume still returns to that exact step.
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(GREETING_STANDING_CHARGE_Q, standing_charge_answer())
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text(STANDING_CHARGE_ANSWER_TEXT)
    assert ui.last_assistant(sid)["citations"][0]["source_id"] == "faq-standing-charge"
    assert ui.disk(sid)["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert ui.disk(sid)["revision"] == 4
    expect(ui.log).not_to_contain_text(ASK_URGENCY)

    # Explicit resume follows the pending clarification before new intake work.
    provider.script_extract(RESUME, x_start_intake())
    assert ui.send(RESUME).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.last_assistant(sid)["text"] == ASK_CATEGORY
    ui.cp("resume_after_restart.explicit-resume-pending-clarification")

    # Finish through review and confirmation on the restarted server.
    provider.script_extract(CATEGORY, x_category())
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    provider.script_extract(HIGH, x_high())
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    for line in SUMMARY.split("\n"):
        expect(ui.review).to_contain_text(line)
    saved = ui.disk(sid)
    assert saved["revision"] == 7 and saved["pending_review_revision"] == 7
    assert ui.confirm().status == 200
    expect(ui.log).to_contain_text(receipt_for(sid))
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 8
    assert saved["summary"] == SUMMARY and saved["confirmation"]["review_revision"] == 7
    confirms = ui.confirm_posts()
    assert len(confirms) == 1 and body_of(confirms[0])["review_revision"] == 7
    assert provider.methods() == [
        "extract",
        "extract",
        "extract",
        "extract",
        "answer_knowledge",
        "extract",
        "extract",
        "extract",
    ]
    assert len(ui.requests("GET", SESSION_ITEM)) >= 4
    wait_until(
        lambda: len(ui.create_posts()) == 1, "no replacement session at any point"
    )
    provider.assert_consumed()
    ui.cp("resume_after_restart.finish-after-restart")
