"""Keyboard and 390x844 mobile use with hostile markup in customer, answer and source text."""

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
    GREETING,
    HIGH,
    RECEIPT_TEMPLATE,
    extraction,
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
SUMMARY = (
    "Account reference: NS-000123\n"
    "Energy support area: equipment issue\n"
    "Issue: My order arrived broken\n"
    "Urgency: high"
)
SPANISH_REVIEW = (
    "Referencia de cuenta: NS-000123\n"
    "Área de asistencia energética: problema con el equipo\n"
    "Problema: My order arrived broken\n"
    "Urgencia: high\n"
    "Confirma estos datos o dime qué cambiar."
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

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


@pytest.mark.parametrize("case_id", [pytest.param("keyboard_mobile", id="keyboard_mobile")])
def test_keyboard_mobile(case_id, server_factory, make_ui, provider, tmp_path):
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
        ui.message.focus()
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
    response = page.goto(server.base_url + "/")
    assert response is not None and response.status == 200
    expect(page).to_have_title("Northstar Energy | Powering everyday life")
    expect(page.get_by_role("dialog", name="Northstar Energy support chat")).to_be_visible()
    expect(page.get_by_role("button", name="Close chat")).to_have_count(0)
    expect(page.locator(".chat-launch")).to_have_count(0)
    expect(page.get_by_role("navigation")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Northstar Energy support chat")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
    chat_box = page.locator("#support-chat").bounding_box()
    compose_box = page.locator(".chat-compose").bounding_box()
    message_box = ui.message.bounding_box()
    assert chat_box is not None and chat_box["width"] == 390
    assert chat_box["height"] == 844
    assert compose_box is not None and message_box is not None
    assert message_box["width"] >= compose_box["width"] * 0.58
    for viewport_width in (320, 390, 768, 802, 1440):
        page.set_viewport_size({"width": viewport_width, "height": 844})
        panel_box = page.locator("#support-chat").bounding_box()
        form_box = page.locator(".chat-compose").bounding_box()
        field_box = ui.message.bounding_box()
        assert panel_box is not None and panel_box["width"] == viewport_width
        assert panel_box["height"] == 844
        assert form_box is not None and field_box is not None
        assert field_box["width"] >= form_box["width"] * 0.58
        assert page.evaluate(NO_OVERFLOW), f"layout must not overflow at {viewport_width}px"
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight"), f"document must not scroll vertically at {viewport_width}px"
    page.set_viewport_size({"width": 390, "height": 844})
    ui.message.fill("")
    ui.expect_greeting()
    sid = ui.current_session_id()
    expect(ui.log).to_have_attribute("aria-live", "polite")
    for name in ("Send", "New conversation", "Start recording", "Attach WAV audio"):
        expect(ui.button(name)).to_be_visible()
    expect(ui.message).to_be_visible()
    expect(ui.file_input).to_be_attached()
    assert page.evaluate(NO_OVERFLOW)
    ui.cp("keyboard_mobile.mobile-named-controls")

    # Enter submits from the message field; Shift+Enter remains a newline.
    ui.message.fill("first line")
    page.keyboard.press("Shift+Enter")
    expect(ui.message).to_have_value("first line\n")
    ui.message.fill("")

    # Literal customer markup and a very long unbroken token render as plain text.
    assert send_with_keyboard(CUSTOMER_PART).status == 200
    expect(ui.log).to_contain_text('<img src=x onerror="window.__pwned=1">')
    expect(ui.log).to_contain_text(LONG_TOKEN)
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert_inert_dom()
    saved = ui.disk(sid)
    assert saved["messages"][1]["text"] == CUSTOMER_PART
    assert body_of(ui.turn_posts()[0])["text"] == CUSTOMER_PART
    ui.cp("keyboard_mobile.customer-markup-escaped")

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
    ui.cp("keyboard_mobile.answer-and-sources-escaped-keyboard")

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
    ui.cp("keyboard_mobile.play-reachable-by-keyboard")

    # The page boots from the server-rendered English catalog: document lang,
    # visible labels, tooltips and accessible names all come from it.
    en_catalog = json.loads(server.request("GET", "/api/locales/en")[2].decode("utf-8"))
    es_catalog = json.loads(server.request("GET", "/api/locales/es")[2].decode("utf-8"))
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(ui.button("Send")).to_have_text(en_catalog["actions.send"])
    expect(ui.button("New conversation")).to_have_attribute(
        "aria-label", en_catalog["actions.new_intake"]
    )
    expect(ui.button(en_catalog["actions.choose_wav"])).to_have_attribute(
        "aria-label", en_catalog["actions.choose_wav"]
    )
    expect(ui.page.locator(".playback__disclosure")).to_have_text(
        f'{en_catalog["speech.disclosure"]} · {en_catalog["site.chat_privacy"]}'
    )
    expect(play).to_have_attribute("aria-label", "Play reply 5")
    expect(play).to_have_attribute("title", "Play reply 5")

    # Switching language updates controls and future replies, never history.
    before_switch = ui.disk(sid)["messages"]
    provider.script_extract(
        "En español",
        extraction(intent="unrelated", detected_language="en", requested_language="es"),
    )
    assert ui.send_localized("En español").status == 200
    expect(page.locator("html")).to_have_attribute("lang", "es")
    expect(ui.button("Enviar")).to_have_text(es_catalog["actions.send"])
    expect(ui.button("Nueva conversación")).to_have_attribute(
        "aria-label", es_catalog["actions.new_intake"]
    )
    expect(ui.button(es_catalog["actions.choose_wav"])).to_have_attribute(
        "aria-label", es_catalog["actions.choose_wav"]
    )
    expect(page.get_by_role("region", name=es_catalog["ui.review_region"])).to_be_visible()
    expect(ui.page.locator(".playback__disclosure")).to_have_text(
        f'{es_catalog["speech.disclosure"]} · {es_catalog["site.chat_privacy"]}'
    )
    expect(ui.play_button(review["id"])).to_have_attribute(
        "aria-label", "Reproducir respuesta 5"
    )
    expect(ui.log).to_contain_text(GREETING)
    expect(ui.log).to_contain_text(SPANISH_REVIEW)
    after_switch = ui.disk(sid)["messages"]
    assert after_switch[: len(before_switch)] == before_switch
    assert_inert_dom()

    provider.script_extract(
        "Continuemos en inglés",
        extraction(intent="unrelated", detected_language="es", requested_language="en"),
    )
    assert ui.send_localized("Continuemos en inglés").status == 200
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(ui.button("Send")).to_have_text(en_catalog["actions.send"])
    expect(ui.log).to_contain_text(SPANISH_REVIEW)

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
    assert saved["status"] == "completed" and saved["revision"] == 7
    assert provider.methods() == [
        "extract", "extract", "answer_knowledge", "extract", "extract", "synthesize",
        "extract", "extract",
    ]  # fmt: skip
    provider.assert_consumed()
    ui.cp("keyboard_mobile.confirm-by-keyboard-no-overflow")
