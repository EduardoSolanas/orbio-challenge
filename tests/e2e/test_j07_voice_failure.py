"""J07: speech failures never block the text path (IC §13.3)."""

from __future__ import annotations

import hashlib

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import body_of
from tests.support.fakes import (
    FULL,
    RECEIPT_TEMPLATE,
    STT_TEXT,
    make_wav,
    review_reply,
    summary_text,
    x_full,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

SUMMARY = summary_text("NS-000123", "damaged_item", "My order arrived broken", "high")
UNAVAILABLE = "The AI service is unavailable. Please retry."


@pytest.mark.parametrize("case_id", [pytest.param("J07", id="J07")])
def test_j07_voice_failure(case_id, ui, server, provider, tmp_path):
    wav = make_wav()
    wav_path = tmp_path / "valid-16k-mono.wav"
    wav_path.write_bytes(wav)
    provider.script_transcribe(
        hashlib.sha256(wav).hexdigest(), STT_TEXT, fail="provider_unavailable"
    )
    provider.script_extract(FULL, x_full())
    provider.script_synthesize(review_reply(SUMMARY), fail="provider_unavailable")

    ui.open()
    ui.expect_greeting()
    sid = ui.current_session_id()
    baseline = server.session_bytes(sid)
    transcript_before = ui.transcript_text()

    # One STT outage: safe error, no draft, no bubble, no commit.
    with ui.page.expect_response(lambda r: r.url.endswith("/api/transcriptions")) as pending:
        ui.upload(wav_path)
    assert pending.value.status == 503
    ui.expect_alert(UNAVAILABLE)
    expect(ui.message).to_have_value("")
    expect(ui.message).to_be_editable()
    expect(ui.button("Start recording")).to_be_enabled()
    expect(ui.button("Attach WAV audio")).to_be_enabled()
    assert ui.transcript_text() == transcript_before
    assert server.session_bytes(sid) == baseline
    assert ui.turn_posts() == []
    assert [p["status"] for p in ui.transcription_posts()] == [503]
    assert [c["outcome"] for c in provider.calls("transcribe")] == ["failed:provider_unavailable"]
    ui.cp("J07.stt-outage-safe-error")

    # Typed FULL still works and is recorded as text input.
    assert ui.send(FULL).status == 200
    sent = ui.turn_posts()[0]
    assert body_of(sent)["input_mode"] == "text" and body_of(sent)["text"] == FULL
    expect(ui.review).to_be_visible()
    saved = ui.disk(sid)
    user_message = [m for m in saved["messages"] if m["role"] == "user"][0]
    assert user_message["input_mode"] == "text" and user_message["text"] == FULL
    assert saved["revision"] == 1 and saved["pending_review_revision"] == 1
    assert saved["summary"] == SUMMARY
    ui.cp("J07.typed-text-mode-review")

    # One TTS outage: safe error, text reply and state untouched.
    reply = ui.last_assistant(sid)
    assert reply["text"] == review_reply(SUMMARY)
    before_play = server.session_bytes(sid)
    with ui.page.expect_response(lambda r: r.url.endswith("/speech")) as pending:
        ui.play_button(reply["id"]).click()
    assert pending.value.status == 503
    ui.expect_alert(UNAVAILABLE)
    expect(ui.log).to_contain_text("Please confirm these details or tell me what to change.")
    expect(ui.review).to_be_visible()
    expect(ui.enabled_button("Confirm details")).to_have_count(1)
    assert server.session_bytes(sid) == before_play
    assert len(ui.turn_posts()) == 1
    assert [c["outcome"] for c in provider.calls("synthesize")] == ["failed:provider_unavailable"]
    ui.cp("J07.tts-outage-text-intact")

    # Confirmation succeeds normally with exactly one receipt.
    assert ui.confirm().status == 200
    receipt = RECEIPT_TEMPLATE.format(session_id=sid)
    expect(ui.log).to_contain_text(receipt)
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 2
    assert [m["text"] for m in saved["messages"]].count(receipt) == 1
    assert len(ui.confirm_posts()) == 1
    assert provider.methods() == ["transcribe", "extract", "synthesize"]
    provider.assert_consumed()
    ui.cp("J07.single-receipt-after-failures")
