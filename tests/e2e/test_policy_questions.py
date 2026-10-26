"""Policy questions during an intake - real retrieval, cited Sources, abstention (IC §13.3, RAG §6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of
from tests.support.fakes import (
    ABSTENTION,
    ASK_CATEGORY,
    ASK_URGENCY,
    CATEGORY,
    GREETING_STANDING_CHARGE_Q,
    HIGH,
    PART,
    PAYMENT_Q,
    STANDING_CHARGE_ANSWER_TEXT,
    receipt_for,
    standing_charge_answer,
    summary_text,
    x_category,
    x_high,
    x_part,
    x_question,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

KNOWLEDGE = json.loads(
    (Path(__file__).resolve().parents[2] / "gateway" / "knowledge" / "northstar-faq.json").read_text(
        encoding="utf-8"
    )
)
STANDING_CHARGE = next(
    record for record in KNOWLEDGE["records"] if record["id"] == "faq-standing-charge"
)
KB_VERSION = KNOWLEDGE["version"]
SNAPSHOT = {
    "source_id": "faq-standing-charge",
    "title": STANDING_CHARGE["title"],
    "excerpt": STANDING_CHARGE["body"],
    "kb_version": KB_VERSION,
}
SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


def assert_sources_open(region) -> None:
    """Open Sources and check the exact energy FAQ snapshot is shown."""

    region.locator("summary").first.click()
    expect(region.locator("details").first).to_have_js_property("open", True)
    expect(region).to_contain_text(STANDING_CHARGE["title"])
    expect(region).to_contain_text("faq-standing-charge")
    expect(region).to_contain_text(STANDING_CHARGE["body"])
    expect(region).to_contain_text(f"Knowledge base version: {KB_VERSION}")


@pytest.mark.parametrize("case_id", [pytest.param("policy_questions", id="policy_questions")])
def test_policy_questions(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(GREETING_STANDING_CHARGE_Q, standing_charge_answer())
    provider.script_extract(PAYMENT_Q, x_question())
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())
    provider.script_extract(GREETING_STANDING_CHARGE_Q, x_question())
    provider.script_answer(GREETING_STANDING_CHARGE_Q, standing_charge_answer())

    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    partial = ui.disk(sid)
    assert partial["revision"] == 1
    ui.cp("policy_questions.partial-intake")

    # A greeting plus policy question follows the policy route, not social or intake.
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(ui.log).to_contain_text("fixed daily amount")
    sources = ui.log.get_by_role("region", name="Sources")
    expect(sources).to_have_count(1)
    assert_sources_open(sources.first)
    saved = ui.disk(sid)
    reply = ui.last_assistant(sid)
    assert reply["text"] == STANDING_CHARGE_ANSWER_TEXT
    assert reply["citations"] == [SNAPSHOT]
    assert saved["revision"] == 2 and saved["collection_turn_count"] == 2
    assert saved["slots"] == partial["slots"] and saved["pending_review_revision"] is None
    answer_call = provider.calls("answer_knowledge")[0]["context"]
    assert answer_call["question"] == GREETING_STANDING_CHARGE_Q and answer_call["kb_version"] == KB_VERSION
    assert answer_call["sources"] == [STANDING_CHARGE]
    ui.cp("policy_questions.return-answer-cited-sources")

    # PAYMENT_Q: deterministic safe abstention only; no missing-field prompt or generation.
    assert ui.send(PAYMENT_Q).status == 200
    expect(ui.log).to_contain_text(ABSTENTION)
    expect(sources).to_have_count(1)
    saved = ui.disk(sid)
    reply = ui.last_assistant(sid)
    assert reply["text"] == ABSTENTION
    assert reply["citations"] == []
    assert saved["revision"] == 3 and saved["slots"] == partial["slots"]
    assert len(provider.calls("answer_knowledge")) == 1
    ui.cp("policy_questions.payment-abstention-no-sources")

    # Finish the intake.
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    saved = ui.disk(sid)
    assert saved["revision"] == 5 and saved["pending_review_revision"] == 5
    assert saved["summary"] == SUMMARY
    ui.cp("policy_questions.intake-finished-review")

    # A policy question at a valid review preserves the token and summary.
    review_slots = saved["slots"]
    assert ui.send(GREETING_STANDING_CHARGE_Q).status == 200
    expect(sources).to_have_count(2)
    expect(ui.review).to_be_visible()
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    reply = ui.last_assistant(sid)
    assert reply["text"] == STANDING_CHARGE_ANSWER_TEXT
    assert reply["citations"] == [SNAPSHOT]
    assert saved["revision"] == 6 and saved["status"] == "awaiting_confirmation"
    assert saved["pending_review_revision"] == 5 and saved["summary"] == SUMMARY
    assert saved["slots"] == review_slots and saved["review_rejected"] is False
    assert len(provider.calls("answer_knowledge")) == 2
    ui.cp("policy_questions.review-question-preserves-token")

    # Confirm with the current revision and the prior review token.
    assert ui.confirm().status == 200
    sent = ui.confirm_posts()
    assert len(sent) == 1
    assert body_of(sent[0])["expected_revision"] == 6
    assert body_of(sent[0])["review_revision"] == 5
    receipt = receipt_for(sid)
    expect(ui.log).to_contain_text(receipt)
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 7
    assert saved["confirmation"]["review_revision"] == 5 and saved["summary"] == SUMMARY
    assert provider.methods() == [
        "extract", "extract", "answer_knowledge", "extract",
        "extract", "extract", "extract", "answer_knowledge",
    ]  # fmt: skip
    ui.cp("policy_questions.confirm-prior-token")

    # Reload: saved Sources restore from the stored snapshots, with no new model call.
    gets_before = len(ui.session_gets())
    ui.page.reload()
    expect(ui.page.get_by_role("dialog", name="Northstar Energy support chat")).to_be_visible()
    expect(ui.log).to_contain_text(receipt)
    restored = ui.log.get_by_role("region", name="Sources")
    expect(restored).to_have_count(2)
    assert_sources_open(restored.last)
    assert len(ui.session_gets()) > gets_before
    assert len(provider.calls("answer_knowledge")) == 2 and len(provider.calls("extract")) == 6
    assert ui.disk(sid)["messages"] == saved["messages"]
    provider.assert_consumed()
    ui.cp("policy_questions.reload-restores-sources")
