"""J09: keyboard and 390x844 mobile use with hostile markup in customer, answer and source text."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from app.schemas import KnowledgeAnswer
from tests.e2e.conftest import body_of
from tests.support.fakes import (
    ASK_CATEGORY,
    ASK_URGENCY,
    CATEGORY,
    HIGH,
    RECEIPT_TEMPLATE,
    review_reply,
    summary_text,
    x_category,
    x_high,
    x_part,
    x_question,
)

HOSTILE_TITLE = 'Returns <img src=x onerror="window.__pwned=1">'
HOSTILE_BODY = (
    "Unused items can be returned. <script>window.__pwned=1</script> <b>bold</b> "
    "[x](javascript:alert(1)) &amp; <a href=\"javascript:window.__pwned=1\">go</a>"
)
HOSTILE_VERSION = "hostile-2026-10-22"
HOSTILE_ANSWER = 'You <b>can</b> return it <img src=x onerror="window.__pwned=1"> within 30 days.'
LONG_TOKEN = "UNBROKEN" * 50
CUSTOMER_PART = (
    '<img src=x onerror="window.__pwned=1"> My order NS-000123 arrived broken. ' + LONG_TOKEN
)
CUSTOMER_QUESTION = "<script>window.__pwned=1</script> Can I return an unused item?"
SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")
NO_OVERFLOW = (
    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
)


def write_hostile_knowledge(path) -> None:
    """Dedicated immutable KB: one valid record whose title/body are literal markup."""

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": HOSTILE_VERSION,
                "records": [
                    {
                        "id": "faq-returns",
                        "title": HOSTILE_TITLE,
                        "keywords": ["return", "returns", "refund", "refunds"],
                        "body": HOSTILE_BODY,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("case_id", [pytest.param("J09", id="J09")])
def test_j09_keyboard_mobile(case_id, server_factory, make_ui, provider, tmp_path):
    knowledge = tmp_path / "hostile-faq.json"
    write_hostile_knowledge(knowledge)
    server = server_factory.start(knowledge_path=knowledge)
    ui = make_ui(server)
    page = ui.page
    mp3_text = review_reply(SUMMARY)

    provider.script_extract(CUSTOMER_PART, x_part())
    provider.script_extract(CUSTOMER_QUESTION, x_question())
    provider.script_answer(
        CUSTOMER_QUESTION,
        KnowledgeAnswer(answerable=True, answer=HOSTILE_ANSWER, cited_ids=["faq-returns"]),
    )
    provider.script_extract(CATEGORY, x_category())
    provider.script_extract(HIGH, x_high())
    provider.script_synthesize(mp3_text)

    def send_with_keyboard(text: str):
        ui.message.fill(text)
        send = ui.button("Send")
        ui.tab_to(send)
        assert ui.focus_is_visible(send), "Send needs a visible keyboard focus indicator"
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/turns")
        ) as pending:
            page.keyboard.press("Enter")
        return pending.value

    def assert_inert_dom() -> None:
        html = ui.log.inner_html()
        assert "<img" not in html and "<script" not in html
        assert ui.log.locator("img, script, iframe, object, embed, [onerror]").count() == 0
        assert page.locator("img[src='x']").count() == 0
        assert page.evaluate("window.__pwned") is None
        assert page.evaluate(NO_OVERFLOW), "document must not scroll horizontally at 390 px"

    page.set_viewport_size({"width": 390, "height": 844})
    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    expect(ui.log).to_have_attribute("aria-live", "polite")
    for name in ("Send", "New intake", "Transcribe audio", "Start recording"):
        expect(ui.button(name)).to_be_visible()
    expect(ui.message).to_be_visible()
    expect(ui.file_input).to_be_attached()
    assert page.evaluate(NO_OVERFLOW)
    ui.cp("J09.mobile-named-controls")

    # Literal customer markup and a very long unbroken token render as plain text.
    assert send_with_keyboard(CUSTOMER_PART).status == 200
    expect(ui.log).to_contain_text('<img src=x onerror="window.__pwned=1">')
    expect(ui.log).to_contain_text(LONG_TOKEN)
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert_inert_dom()
    saved = ui.disk(sid)
    assert saved["messages"][1]["text"] == CUSTOMER_PART
    assert body_of(ui.turn_posts()[0])["text"] == CUSTOMER_PART
    ui.cp("J09.customer-markup-escaped")

    # Supported policy answer containing markup: answer text and Sources stay literal.
    assert send_with_keyboard(CUSTOMER_QUESTION).status == 200
    expect(ui.log).to_contain_text(HOSTILE_ANSWER)
    sources = ui.log.get_by_role("region", name="Sources")
    expect(sources).to_have_count(1)
    summary = sources.first.locator("summary").first
    ui.tab_to(summary)
    assert ui.focus_is_visible(summary), "Sources summary needs a visible focus indicator"
    page.keyboard.press("Enter")
    expect(sources.first.locator("details").first).to_have_js_property("open", True)
    expect(sources.first).to_contain_text(HOSTILE_TITLE)
    expect(sources.first).to_contain_text("faq-returns")
    expect(sources.first).to_contain_text(HOSTILE_BODY)
    expect(sources.first).to_contain_text(f"Knowledge base version: {HOSTILE_VERSION}")
    assert_inert_dom()
    reply = ui.last_assistant(sid)
    assert reply["citations"] == [
        {
            "source_id": "faq-returns",
            "title": HOSTILE_TITLE,
            "excerpt": HOSTILE_BODY,
            "kb_version": HOSTILE_VERSION,
        }
    ]
    assert provider.calls("answer_knowledge")[0]["context"]["sources"][0]["body"] == HOSTILE_BODY
    ui.cp("J09.answer-and-sources-escaped-keyboard")

    # Finish the intake and reach Play and Confirm with the keyboard alone.
    assert send_with_keyboard(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    assert send_with_keyboard(HIGH).status == 200
    expect(ui.review).to_be_visible()
    assert ui.disk(sid)["summary"] == SUMMARY
    assert_inert_dom()
    review = ui.last_assistant(sid)
    assert review["text"] == mp3_text
    play = ui.play_button(review["id"])
    ui.tab_to(play)
    assert ui.focus_is_visible(play), "Play needs a visible keyboard focus indicator"
    with page.expect_response(lambda r: r.url.endswith("/speech")) as pending:
        page.keyboard.press("Enter")
    assert pending.value.status == 200
    expect(ui.audio).to_have_attribute("src", re.compile(r"^blob:"))
    ui.cp("J09.play-reachable-by-keyboard")

    confirm = ui.button("Confirm details")
    ui.tab_to(confirm)
    assert ui.focus_is_visible(confirm), "Confirm needs a visible keyboard focus indicator"
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
    ) as pending:
        page.keyboard.press("Enter")
    assert pending.value.status == 200
    expect(ui.log).to_contain_text(RECEIPT_TEMPLATE.format(session_id=sid))
    assert_inert_dom()
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 5
    assert provider.methods() == [
        "extract", "extract", "answer_knowledge", "extract", "extract", "synthesize",
    ]  # fmt: skip
    provider.assert_consumed()
    ui.cp("J09.confirm-by-keyboard-no-overflow")
