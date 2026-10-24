"""Stale STT and TTS results from an older generation or token are ignored (IC §13.3)."""

from __future__ import annotations

import hashlib
import json
import re

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
    STT_TEXT,
    Gate,
    make_wav,
    x_category,
    x_high,
    x_part,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

NEXT_TASK = "() => new Promise(resolve => setTimeout(resolve, 0))"
PLAYING = """() => {
  const audio = document.querySelector('[data-testid="reply-audio"]');
  return !!audio && audio.duration > 0 && audio.currentTime > 0;
}"""


@pytest.mark.parametrize("case_id", [pytest.param("stale_voice_results", id="stale_voice_results")])
def test_stale_voice_results(case_id, ui, server, provider, tmp_path):
    wav = make_wav()
    wav_path = tmp_path / "valid-16k-mono.wav"
    wav_path.write_bytes(wav)
    stt_gate = Gate("old-generation STT")
    tts_a_gate = Gate("TTS A")
    provider.script_transcribe(hashlib.sha256(wav).hexdigest(), STT_TEXT, hold=stt_gate)
    provider.script_extract(PART, x_part())
    provider.script_extract(CATEGORY, x_category())
    provider.script_synthesize(ASK_CATEGORY, hold=tts_a_gate)  # reply A
    provider.script_synthesize(ASK_URGENCY)  # reply B
    provider.script_extract(HIGH, x_high())

    ui.open()
    ui.expect_greeting()
    old_sid = ui.current_session_id()
    old_bytes = server.session_bytes(old_sid)

    # WAV attachment starts STT automatically. Lock every chat button until it ends.
    with ui.page.expect_response(lambda r: r.url.endswith("/api/transcriptions")) as pending:
        ui.upload(wav_path)
        stt_gate.wait_entered(timeout=2)
        expect(ui.message).to_have_js_property("readOnly", True)
        assert ui.page.locator("#support-chat button").evaluate_all(
            "buttons => buttons.length > 0 && buttons.every(button => button.disabled)"
        )
        assert len(ui.transcription_posts()) == 1 and ui.transcription_posts()[0]["done"] is False
        ui.cp("stale_voice_results.stt-locks-chat-controls")
        stt_gate.release()
    assert pending.value.status == 200
    expect(ui.message).to_have_value(STT_TEXT)
    expect(ui.message).to_be_editable()
    provider.wait_finished("transcribe", 1)
    assert server.session_bytes(old_sid) == old_bytes
    ui.cp("stale_voice_results.auto-stt-fills-editable-draft")

    # A new intake becomes available once transcription releases its lock.
    assert ui.new_intake().status == 201
    wait_until(lambda: ui.stored_session_id() != old_sid, "new session id in localStorage")
    new_sid = ui.current_session_id()
    new_bytes = server.session_bytes(new_sid)
    ui.page.evaluate(NEXT_TASK)
    expect(ui.message).to_have_value("")
    expect(ui.message).to_be_editable()
    ui.expect_no_alert()
    expect(ui.log).to_contain_text(GREETING)
    expect(ui.log).not_to_contain_text(STT_TEXT)
    assert ui.stored_session_id() == new_sid != old_sid
    assert server.session_bytes(old_sid) == old_bytes
    assert server.session_bytes(new_sid) == new_bytes
    assert ui.disk(new_sid)["revision"] == 0 and ui.turn_posts() == []
    assert [c["outcome"] for c in provider.calls("transcribe")] == ["ok"]
    ui.cp("stale_voice_results.new-intake-after-stt-unlock")

    # New intake: two saved replies A and B.
    assert ui.send(PART).status == 200
    expect(ui.log).to_contain_text(ASK_CATEGORY)
    assert ui.send(CATEGORY).status == 200
    expect(ui.log).to_contain_text(ASK_URGENCY)
    replies = ui.assistant_messages(new_sid)
    reply_a, reply_b = replies[1], replies[2]
    assert (reply_a["text"], reply_b["text"]) == (ASK_CATEGORY, ASK_URGENCY)
    before_audio = server.session_bytes(new_sid)
    ui.cp("stale_voice_results.saved-replies-a-b")

    # Hold TTS A, Play B completes, then A completes late: only B owns the audio.
    ui.play_button(reply_a["id"]).click()
    tts_a_gate.wait_entered()
    with ui.page.expect_response(
        lambda r: r.url.endswith("/speech")
        and json.loads(r.request.post_data or "{}").get("message_id") == reply_b["id"]
    ) as pending:
        ui.play_button(reply_b["id"]).click()
    assert pending.value.status == 200
    expect(ui.audio).to_have_attribute("src", re.compile(r"^blob:"))
    ui.page.wait_for_function(PLAYING)
    src_b = ui.audio.get_attribute("src")
    tts_a_gate.release()
    provider.wait_finished("synthesize", 2)
    ui.page.evaluate(NEXT_TASK)
    assert [body_of(p)["message_id"] for p in ui.speech_posts()] == [reply_a["id"], reply_b["id"]]
    assert [c["text"] for c in provider.calls("synthesize")] == [ASK_CATEGORY, ASK_URGENCY]
    expect(ui.audio).to_have_attribute("src", src_b)
    spy = ui.spy()
    created = [c["url"] for c in spy["created"]]
    assert src_b in created and src_b not in spy["revoked"]
    assert set(created) - set(spy["revoked"]) == {src_b}, "stale audio URLs must be revoked"
    assert all(c["type"] == "audio/mpeg" for c in spy["created"])
    assert server.session_bytes(new_sid) == before_audio
    ui.cp("stale_voice_results.only-latest-tts-owns-audio")

    # Send stays usable during playback.
    assert ui.send(HIGH).status == 200
    expect(ui.review).to_be_visible()
    expect(ui.audio).to_have_attribute("src", src_b)
    assert ui.disk(new_sid)["revision"] == 3
    ui.cp("stale_voice_results.send-usable-during-playback")

    # A real pagehide event pauses and revokes the assigned audio.
    ui.page.evaluate(
        "() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: false}))"
    )
    expect(ui.audio).to_have_js_property("paused", True)
    wait_until(lambda: src_b in ui.spy()["revoked"], "assigned audio URL revoked on pagehide")
    assert provider.methods() == [
        "transcribe", "extract", "extract", "synthesize", "synthesize", "extract",
    ]  # fmt: skip
    provider.assert_consumed()
    ui.cp("stale_voice_results.pagehide-pauses-and-revokes")
