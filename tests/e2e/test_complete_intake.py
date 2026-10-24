"""Complete an intake, reload the completed view, start a new intake (IC §13.3)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from playwright.sync_api import expect

from app.localization import text
from tests.e2e.conftest import body_of, response_of, wait_until
from tests.support.fakes import (
    ASK_CATEGORY,
    ASK_URGENCY,
    AUTH_DEMO_CODE_NOTICE,
    AUTH_ORDER_RESULT,
    AUTH_VERIFICATION_FAILED,
    AUTH_VERIFICATION_PROMPT,
    AUTH_VERIFIED,
    CATEGORY,
    GREETING_STANDING_CHARGE_Q,
    GREETING,
    HIGH,
    MIXED_PART,
    ORDER_STATUS_Q,
    RECEIPT_TEMPLATE,
    THANKS,
    STANDING_CHARGE_ANSWER_TEXT,
    standing_charge_answer,
    review_reply,
    summary_text,
    x_category,
    x_gratitude,
    x_high,
    x_order_lookup,
    x_part,
    x_question,
    x_social,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

SUMMARY = (
    "Account reference: NS-000123\n"
    "Energy support area: equipment issue\n"
    "Issue: My order arrived broken\n"
    "Urgency: high"
)


@pytest.mark.parametrize("case_id", [pytest.param("complete_intake", id="complete_intake")])
def test_complete_intake(case_id, ui, server, provider):
    provider.script_extract(THANKS, x_gratitude())
    provider.script_extract(MIXED_PART, x_part())
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
    ui.cp("complete_intake.fresh-greeting")

    # Social thanks resumes the same UX without retrieval or an intake prompt.
    assert ui.send(THANKS).status == 200
    expect(ui.log).to_contain_text("You're welcome! How else can I help with your energy service?")
    expect(ui.log).not_to_contain_text(ASK_CATEGORY)
    assert ui.last_assistant(sid)["text"] == "You're welcome! How else can I help with your energy service?"
    assert ui.disk(sid)["slots"] == {
        "order_number": None,
        "category": None,
        "description": None,
        "urgency": None,
    }
    assert provider.methods() == ["extract"]
    ui.cp("complete_intake.social-acknowledgment-no-intake-prompt")

    # A greeting plus facts is substantive intake, not social conversation.
    assert ui.send(MIXED_PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    expect(ui.message).to_have_value("")
    expect(ui.review).to_be_hidden()
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    saved = ui.disk(sid)
    assert saved["revision"] == 2 and saved["collection_turn_count"] == 2
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert saved["pending_review_revision"] is None and saved["summary"] is None
    assert ui.last_assistant(sid)["text"] == ASK_CATEGORY
    first = ui.turn_posts()[1]
    assert first["status"] == 200
    assert body_of(first)["text"] == MIXED_PART
    assert body_of(first)["expected_revision"] == 1
    assert body_of(first)["input_mode"] == "text"
    ui.cp("complete_intake.mixed-greeting-facts-start-intake")

    # CATEGORY: asks for urgency.
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    saved = ui.disk(sid)
    assert saved["revision"] == 3 and saved["slots"]["category"] == "damaged_item"
    assert saved["pending_review_revision"] is None
    ui.cp("complete_intake.category-asks-urgency")

    # HIGH: review with exactly the four slots, bound to the resulting revision.
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    for line in SUMMARY.split("\n"):
        expect(ui.review).to_contain_text(line)
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    assert saved["revision"] == 4 and saved["status"] == "awaiting_confirmation"
    assert saved["pending_review_revision"] == 4
    assert saved["summary"] == SUMMARY
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": "high",
    }
    assert ui.last_assistant(sid)["text"] == review_reply(SUMMARY)
    ui.cp("complete_intake.review-four-slots")

    # Confirm details: dedicated confirmation, one local-only receipt, no model call.
    response = ui.confirm()
    assert response.status == 200
    receipt = RECEIPT_TEMPLATE.format(session_id=sid)
    expect(ui.log).to_contain_text(receipt)
    sent = ui.confirm_posts()
    assert len(sent) == 1 and sent[0]["status"] == 200
    assert body_of(sent[0])["expected_revision"] == 4
    assert body_of(sent[0])["review_revision"] == 4
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 5
    assert saved["collection_turn_count"] == 4
    assert saved["confirmation"]["review_revision"] == 4
    assert saved["summary"] == SUMMARY
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert provider.methods() == ["extract", "extract", "extract", "extract"]
    expect(ui.message).not_to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(0)
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    ui.cp("complete_intake.confirmed-local-receipt")

    # Reload: completed view restored by GET, read-only, no model call, no commit.
    completed_bytes = server.session_bytes(sid)
    gets_before = len(ui.session_gets())
    ui.page.reload()
    expect(ui.page.get_by_role("dialog", name="Northstar Energy support chat")).to_be_visible()
    expect(ui.log).to_contain_text(receipt)
    expect(ui.message).not_to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(0)
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    wait_until(lambda: len(ui.session_gets()) > gets_before, "resume GET after reload")
    resumed = ui.session_gets()[-1]
    assert resumed["status"] == 200 and resumed["path"].endswith(sid)
    assert len(ui.create_posts()) == 1
    assert provider.methods() == ["extract", "extract", "extract", "extract"]
    assert server.session_bytes(sid) == completed_bytes
    ui.cp("complete_intake.reload-completed-read-only")

    # New intake: new ID, empty intake, previous saved file untouched.
    assert ui.new_intake().status == 201
    wait_until(lambda: ui.stored_session_id() != sid, "new session id in localStorage")
    new_sid = ui.current_session_id()
    assert new_sid != sid and str(UUID(new_sid)) == new_sid
    expect(ui.log).to_contain_text(GREETING)
    expect(ui.log).not_to_contain_text(receipt)
    expect(ui.log).not_to_contain_text(MIXED_PART)
    expect(ui.message).to_be_editable()
    expect(ui.enabled_button("Confirm details")).to_have_count(0)
    assert server.session_ids() == sorted([sid, new_sid])
    assert server.session_bytes(sid) == completed_bytes
    fresh = ui.disk(new_sid)
    assert fresh["revision"] == 0 and fresh["status"] == "collecting"
    assert [m["text"] for m in fresh["messages"]] == [GREETING]
    assert len(ui.create_posts()) == 2
    ui.cp("complete_intake.new-intake-isolated")

    # Report an issue first: volunteered issue details need no verification.
    provider.script_extract(MIXED_PART, x_part())
    assert ui.send(MIXED_PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    expect(ui.verification).to_be_hidden()
    saved = ui.disk(new_sid)
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert saved["pending_review_revision"] is None
    ui.cp("complete_intake.issue-reported-without-verification")

    # Protected order lookup: the intake order becomes pending and no data is
    # returned until the demo credentials are verified.
    provider.script_extract(ORDER_STATUS_Q, x_order_lookup())
    assert ui.send(ORDER_STATUS_Q).status == 200
    expect(ui.verification).to_be_visible()
    expect(ui.verification_notice).to_have_text(AUTH_DEMO_CODE_NOTICE)
    expect(ui.log).to_contain_text(AUTH_VERIFICATION_PROMPT)
    expect(ui.log).not_to_contain_text(AUTH_ORDER_RESULT)
    expect(ui.message).to_be_editable()
    expect(ui.enabled_button("Send")).to_have_count(0)
    assert AUTH_DEMO_CODE_NOTICE not in ui.transcript_text()
    assert "SW1A" not in ui.transcript_text()
    assert ui.disk(new_sid)["slots"]["order_number"] == "NS-000123"
    ui.cp("complete_intake.verification-form-demo-notice")

    wrong = ui.verify_order("SW1A 1AA", "123456")
    assert wrong.status == 200
    assert json.loads(wrong.text()) == {
        "verified": False,
        "error": "verification_failed",
    }
    expect(ui.verification_error).to_be_visible()
    expect(ui.verification_error).to_have_text(AUTH_VERIFICATION_FAILED)
    expect(ui.verification).to_be_visible()
    expect(ui.message).to_be_editable()
    assert "123456" not in ui.last_assistant(new_sid)["text"]
    assert "SW1A" not in ui.transcript_text()
    ui.cp("complete_intake.wrong-code-localized-error")

    # A FAQ remains available while account verification is pending. Answering
    # it must preserve the pending verification form and intake data.
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(
        GREETING_STANDING_CHARGE_Q, standing_charge_answer()
    )
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text(STANDING_CHARGE_ANSWER_TEXT)
    expect(ui.verification).to_be_visible()
    expect(ui.message).to_be_editable()
    assert response_of(ui.turn_posts()[-1])["order_verification_required"] is True
    assert ui.last_assistant(new_sid)["citations"][0]["source_id"] == "faq-standing-charge"
    assert ui.disk(new_sid)["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert len(ui.verification_posts()) == 1

    # the Order API is a separate private process. Stop it, submit valid
    # credentials and assert the gateway returns the fixed safe error without
    # changing the pending order; restart on the same origin and recover.
    server.stop_order_api()
    outage = ui.verify_order("SW1A 1AA", "246810")
    assert outage.status == 503
    expect(ui.verification_error).to_have_text(
        text("errors.order_api_unavailable", "en")
    )
    expect(ui.verification).to_be_visible()
    expect(ui.verification_status).to_be_hidden()
    expect(ui.message).to_be_editable()
    assert ui.disk(new_sid)["slots"]["order_number"] == "NS-000123"
    assert len(ui.verification_posts()) == 2
    ui.cp("complete_intake.order-api-outage-safe-error")

    server.start_order_api()
    correct = ui.verify_order(" sw1a 1aa ", "246810")
    assert correct.status == 200
    assert json.loads(correct.text()) == {"verified": True, "error": None}
    expect(ui.verification).to_be_hidden()
    expect(ui.verification_status).to_contain_text(AUTH_VERIFIED)
    expect(ui.message).to_be_editable()
    assert len(ui.verification_posts()) == 3
    ui.cp("complete_intake.demo-code-success-enables-composer")

    # FAQ is still a side path after verification and before the protected
    # lookup; it must not consume or weaken the order-specific grant.
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(
        GREETING_STANDING_CHARGE_Q, standing_charge_answer()
    )
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text(STANDING_CHARGE_ANSWER_TEXT)
    expect(ui.verification).to_be_hidden()
    assert ui.last_assistant(new_sid)["citations"][0]["source_id"] == "faq-standing-charge"
    assert response_of(ui.turn_posts()[-1])["order_verification_required"] is False
    assert ui.disk(new_sid)["slots"]["order_number"] == "NS-000123"

    provider.script_extract(
        ORDER_STATUS_Q, x_order_lookup(order_number="NS-000123", tool=True)
    )
    assert ui.send(ORDER_STATUS_Q).status == 200
    expect(ui.log).to_contain_text(AUTH_ORDER_RESULT)
    assert ui.last_assistant(new_sid)["text"] == AUTH_ORDER_RESULT
    assert ui.disk(new_sid)["slots"]["order_number"] == "NS-000123"
    ui.cp("complete_intake.verified-allowlisted-order-summary")

    # A FAQ after the protected result returns to the still-pending issue step.
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(
        GREETING_STANDING_CHARGE_Q, standing_charge_answer()
    )
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text(STANDING_CHARGE_ANSWER_TEXT)
    assert ui.last_assistant(new_sid)["citations"][0]["source_id"] == "faq-standing-charge"
    assert ui.disk(new_sid)["slots"] == {
        "order_number": "NS-000123",
        "category": None,
        "description": "My order arrived broken",
        "urgency": None,
    }

    # The verified order never skips the normal issue interview.
    provider.script_extract(CATEGORY, x_category())
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    expect(ui.verification).to_be_hidden()
    expect(ui.review).to_be_hidden()
    saved = ui.disk(new_sid)
    assert saved["slots"] == {
        "order_number": "NS-000123",
        "category": "damaged_item",
        "description": "My order arrived broken",
        "urgency": None,
    }
    assert saved["pending_review_revision"] is None
    serialized = json.dumps(saved)
    for secret in (
        "SW1A",
        '"123456"',
        "246810",
        '"delivery_postcode"',
        '"verification_code"',
    ):
        assert secret not in serialized
    ui.cp("complete_intake.verified-order-still-collects-issue-fields")

    provider.script_extract(HIGH, x_high())
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    expect(ui.log).to_contain_text(review_reply(summary_text(
        "NS-000123", "damaged_item", "My order arrived broken", "high"
    )))
    assert ui.disk(new_sid)["pending_review_revision"] == 8
    ui.cp("complete_intake.verified-order-review")

    assert ui.confirm().status == 200
    new_receipt = RECEIPT_TEMPLATE.format(session_id=new_sid)
    expect(ui.log).to_contain_text(new_receipt)
    completed = ui.disk(new_sid)
    assert completed["status"] == "completed"
    assert completed["slots"]["order_number"] == "NS-000123"
    assert completed["summary"] == summary_text(
        "NS-000123", "damaged_item", "My order arrived broken", "high"
    )
    serialized = json.dumps(completed)
    for secret in (
        "SW1A",
        '"123456"',
        "246810",
        '"delivery_postcode"',
        '"verification_code"',
        '"verified_order_number"',
    ):
        assert secret not in serialized
    assert len(provider.calls("extract")) == 12
    ui.cp("complete_intake.verified-order-confirmed")

    provider.assert_consumed()
