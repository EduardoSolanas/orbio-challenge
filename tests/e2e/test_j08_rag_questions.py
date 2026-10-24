"""J08: policy questions during an intake - real retrieval, cited Sources, abstention (IC §13.3, RAG §6)."""

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
    GREETING_RETURN_Q,
    HIGH,
    PART,
    PAYMENT_Q,
    RECEIPT_TEMPLATE,
    RETURN_ANSWER_TEXT,
    RETURN_Q,
    return_answer,
    summary_text,
    x_category,
    x_high,
    x_part,
    x_question,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

KNOWLEDGE = json.loads(
    (Path(__file__).resolve().parents[2] / "app" / "knowledge" / "northstar-faq.json").read_text(
        encoding="utf-8"
    )
)
RETURNS = next(record for record in KNOWLEDGE["records"] if record["id"] == "faq-returns")
KB_VERSION = KNOWLEDGE["version"]
SNAPSHOT = {
    "source_id": "faq-returns",
    "title": RETURNS["title"],
    "excerpt": RETURNS["body"],
    "kb_version": KB_VERSION,
}
SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")


def assert_sources_open(region) -> None:
    """Open the Sources details and check the exact faq-returns snapshot is shown."""

    region.locator("summary").first.click()
    expect(region.locator("details").first).to_have_js_property("open", True)
    expect(region).to_contain_text(RETURNS["title"])
    expect(region).to_contain_text("faq-returns")
    expect(region).to_contain_text(RETURNS["body"])
    expect(region).to_contain_text(f"Knowledge base version: {KB_VERSION}")


@pytest.mark.parametrize("case_id", [pytest.param("J08", id="J08")])
def test_j08_rag_questions(case_id, ui, server, provider):
    provider.script_extract(PART, x_part())
    provider.script_extract(GREETING_RETURN_Q, x_question())
    provider.script_answer(GREETING_RETURN_Q, return_answer())
    provider.script_extract(PAYMENT_Q, x_question())
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())
    provider.script_extract(RETURN_Q, x_question())
    provider.script_answer(RETURN_Q, return_answer())

    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    partial = ui.disk(sid)
    assert partial["revision"] == 1
    ui.cp("J08.partial-intake")

    # A greeting plus policy question follows the policy route, not social or intake.
    assert ui.send(GREETING_RETURN_Q).status == 200
    expect(ui.log).to_contain_text(RETURN_ANSWER_TEXT)
    sources = ui.log.get_by_role("region", name="Sources")
    expect(sources).to_have_count(1)
    assert_sources_open(sources.first)
    saved = ui.disk(sid)
    reply = ui.last_assistant(sid)
    assert reply["text"] == RETURN_ANSWER_TEXT
    assert reply["citations"] == [SNAPSHOT]
    assert saved["revision"] == 2 and saved["collection_turn_count"] == 2
    assert saved["slots"] == partial["slots"] and saved["pending_review_revision"] is None
    answer_call = provider.calls("answer_knowledge")[0]["context"]
    assert answer_call["question"] == GREETING_RETURN_Q and answer_call["kb_version"] == KB_VERSION
    assert answer_call["sources"] == [RETURNS]
    ui.cp("J08.return-answer-cited-sources")

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
    ui.cp("J08.payment-abstention-no-sources")

    # Finish the intake.
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    saved = ui.disk(sid)
    assert saved["revision"] == 5 and saved["pending_review_revision"] == 5
    assert saved["summary"] == SUMMARY
    ui.cp("J08.intake-finished-review")

    # RETURN_Q at a valid review: revision advances, token/summary/slots preserved.
    review_slots = saved["slots"]
    assert ui.send(RETURN_Q).status == 200
    expect(sources).to_have_count(2)
    expect(ui.review).to_be_visible()
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    saved = ui.disk(sid)
    reply = ui.last_assistant(sid)
    assert reply["text"] == RETURN_ANSWER_TEXT
    assert reply["citations"] == [SNAPSHOT]
    assert saved["revision"] == 6 and saved["status"] == "awaiting_confirmation"
    assert saved["pending_review_revision"] == 5 and saved["summary"] == SUMMARY
    assert saved["slots"] == review_slots and saved["review_rejected"] is False
    assert len(provider.calls("answer_knowledge")) == 2
    ui.cp("J08.review-question-preserves-token")

    # Confirm with the current revision and the prior review token.
    assert ui.confirm().status == 200
    sent = ui.confirm_posts()
    assert len(sent) == 1
    assert body_of(sent[0])["expected_revision"] == 6
    assert body_of(sent[0])["review_revision"] == 5
    receipt = RECEIPT_TEMPLATE.format(session_id=sid)
    expect(ui.log).to_contain_text(receipt)
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 7
    assert saved["confirmation"]["review_revision"] == 5 and saved["summary"] == SUMMARY
    assert provider.methods() == [
        "extract", "extract", "answer_knowledge", "extract",
        "extract", "extract", "extract", "answer_knowledge",
    ]  # fmt: skip
    ui.cp("J08.confirm-prior-token")

    # Reload: saved Sources restore from the stored snapshots, with no new model call.
    gets_before = len(ui.session_gets())
    ui.page.reload()
    expect(ui.log).to_contain_text(receipt)
    restored = ui.log.get_by_role("region", name="Sources")
    expect(restored).to_have_count(2)
    assert_sources_open(restored.last)
    assert len(ui.session_gets()) > gets_before
    assert len(provider.calls("answer_knowledge")) == 2 and len(provider.calls("extract")) == 6
    assert ui.disk(sid)["messages"] == saved["messages"]
    provider.assert_consumed()
    ui.cp("J08.reload-restores-sources")
