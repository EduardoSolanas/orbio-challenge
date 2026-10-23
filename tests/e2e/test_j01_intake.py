"""J01: complete an intake, reload the completed view, start a new intake (IC §13.3)."""

from __future__ import annotations

from uuid import UUID

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of, wait_until
from tests.support.fakes import (
    ASK_CATEGORY,
    ASK_URGENCY,
    CATEGORY,
    GREETING,
    HIGH,
    PART,
    RECEIPT_TEMPLATE,
    review_reply,
    summary_text,
    x_category,
    x_high,
    x_part,
)

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


@pytest.mark.parametrize("case_id", [pytest.param("J01", id="J01")])
def test_j01_intake(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())

    # Fresh page: greeting persisted, one session created, no model call.
    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    assert str(UUID(sid)) == sid
    creates = ui.create_posts()
    assert [c["status"] for c in creates] == [201]
    assert creates[0]["request_body"] in (b"", b"{}")
    saved = ui.disk(sid)
    assert saved["revision"] == 0 and saved["status"] == "collecting"
    assert [m["text"] for m in saved["messages"]] == [GREETING]
    assert provider.methods() == []
    ui.cp("J01.fresh-greeting")

    # PART: natural missing-field prompt for the category.
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(PART)
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    expect(ui.message).to_have_value("")
    expect(ui.review).to_be_hidden()
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    saved = ui.disk(sid)
    assert saved["revision"] == 1 and saved["collection_turn_count"] == 1
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert saved["pending_review_revision"] is None and saved["summary"] is None
    assert ui.last_assistant(sid)["text"] == ASK_CATEGORY
    first = ui.turn_posts()[0]
    assert first["status"] == 200
    assert body_of(first)["text"] == PART
    assert body_of(first)["expected_revision"] == 0
    assert body_of(first)["input_mode"] == "text"
    ui.cp("J01.part-asks-category")

    # CATEGORY: asks for urgency.
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    saved = ui.disk(sid)
    assert saved["revision"] == 2 and saved["slots"]["category"] == "damaged_item"
    assert saved["pending_review_revision"] is None
    ui.cp("J01.category-asks-urgency")

    # HIGH: review with exactly the four slots, bound to the resulting revision.
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    for line in SUMMARY.split("\n"):
        expect(ui.review).to_contain_text(line)
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    assert saved["revision"] == 3 and saved["status"] == "awaiting_confirmation"
    assert saved["pending_review_revision"] == 3
    assert saved["summary"] == SUMMARY
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": "high",
    }
    assert ui.last_assistant(sid)["text"] == review_reply(SUMMARY)
    ui.cp("J01.review-four-slots")

    # Confirm details: dedicated confirmation, one local-only receipt, no model call.
    response = ui.confirm()
    assert response.status == 200
    receipt = RECEIPT_TEMPLATE.format(session_id=sid)
    expect(ui.log).to_contain_text(receipt)
    sent = ui.confirm_posts()
    assert len(sent) == 1 and sent[0]["status"] == 200
    assert body_of(sent[0])["expected_revision"] == 3
    assert body_of(sent[0])["review_revision"] == 3
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 4
    assert saved["collection_turn_count"] == 3
    assert saved["confirmation"]["review_revision"] == 3
    assert saved["summary"] == SUMMARY
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert provider.methods() == ["extract", "extract", "extract"]
    expect(ui.message).not_to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(0)
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    ui.cp("J01.confirmed-local-receipt")

    # Reload: completed view restored by GET, read-only, no model call, no commit.
    completed_bytes = server.session_bytes(sid)
    gets_before = len(ui.session_gets())
    ui.page.reload()
    expect(ui.log).to_contain_text(receipt)
    expect(ui.message).not_to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(0)
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    wait_until(lambda: len(ui.session_gets()) > gets_before, "resume GET after reload")
    resumed = ui.session_gets()[-1]
    assert resumed["status"] == 200 and resumed["path"].endswith(sid)
    assert len(ui.create_posts()) == 1
    assert provider.methods() == ["extract", "extract", "extract"]
    assert server.session_bytes(sid) == completed_bytes
    ui.cp("J01.reload-completed-read-only")

    # New intake: new ID, empty intake, previous saved file untouched.
    assert ui.new_intake().status == 201
    wait_until(lambda: ui.stored_session_id() != sid, "new session id in localStorage")
    new_sid = ui.current_session_id()
    assert new_sid != sid and str(UUID(new_sid)) == new_sid
    expect(ui.log).to_contain_text(GREETING)
    expect(ui.log).not_to_contain_text(receipt)
    expect(ui.log).not_to_contain_text(PART)
    expect(ui.message).to_be_editable()
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    assert server.session_ids() == sorted([sid, new_sid])
    assert server.session_bytes(sid) == completed_bytes
    fresh = ui.disk(new_sid)
    assert fresh["revision"] == 0 and fresh["status"] == "collecting"
    assert [m["text"] for m in fresh["messages"]] == [GREETING]
    assert len(ui.create_posts()) == 2
    provider.assert_consumed()
    ui.cp("J01.new-intake-isolated")
