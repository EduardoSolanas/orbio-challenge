"""Voice end to end - recording take, WAV upload, transcript review, MP3 playback (IC §13.3)."""

from __future__ import annotations

import hashlib
import re
import wave
from io import BytesIO

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import DENY_MICROPHONE_JS, body_of
from tests.support.fakes import (
    RECEIPT_TEMPLATE,
    STT_TEXT,
    Gate,
    make_wav,
    reply_mp3_bytes,
    review_reply,
    summary_text,
    x_full,
)

pytestmark = pytest.mark.browser_e2e  # controlled-provider browser workflow, never live Docker/provider proof

EDITED = STT_TEXT.replace("NS-000123", "NS-000456")
SUMMARY = summary_text("NS-000456", "damaged_item", "My order arrived broken", "high")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PLAYBACK_READY = """() => {
  const audio = document.querySelector('[data-testid="reply-audio"]');
  return !!audio && Number.isFinite(audio.duration) && audio.duration > 0 && audio.currentTime > 0;
}"""


def assert_accepted_pcm_wav(audio: bytes) -> None:
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    assert 0 < len(audio) <= MAX_UPLOAD_BYTES
    with wave.open(BytesIO(audio)) as reader:
        channels, width, rate = reader.getnchannels(), reader.getsampwidth(), reader.getframerate()
        count = reader.getnframes()
        frames = reader.readframes(count)
    assert channels in (1, 2) and width == 2 and 8000 <= rate <= 48000
    assert count > 0 and count / rate <= 60
    assert len(frames) == count * channels * 2
    peak = max(
        abs(int.from_bytes(frames[i : i + 2], "little", signed=True))
        for i in range(0, len(frames), 2)
    )
    assert peak >= 128, "recorded take must not be silent"


@pytest.mark.parametrize("case_id", [pytest.param("voice_success", id="voice_success")])
def test_voice_success(case_id, ui, server, provider, tmp_path):
    wav = make_wav()
    wav_path = tmp_path / "valid-16k-mono.wav"
    wav_path.write_bytes(wav)
    mp3 = reply_mp3_bytes()
    recording_gate = Gate("recording STT")
    provider.script_transcribe(None, STT_TEXT, hold=recording_gate)  # Chromium fake-device take
    provider.script_transcribe(hashlib.sha256(wav).hexdigest(), STT_TEXT)  # valid WAV upload
    provider.script_extract(EDITED, x_full("NS-000456"))
    provider.script_synthesize(review_reply(SUMMARY))

    ui.open()
    ui.expect_greeting()
    assert ui.audio.get_attribute("controls") is None, "reply audio must use the inline speaker, not browser-native controls"
    expect(ui.audio).to_be_hidden()
    assert ui.page.locator("#message-form #start-recording").count() == 1, "recording belongs beside the message input"
    assert ui.page.locator("#message-form #stop-recording").count() == 1, "stop belongs beside the message input"
    expect(ui.button("Start recording").locator("svg[aria-hidden='true']")).to_have_count(1)
    sid = ui.current_session_id()
    baseline = server.session_bytes(sid)
    transcript_before = ui.transcript_text()

    # Microphone denial in a separate page: alert, text and upload stay usable.
    denied = ui.new_page()
    denied.add_init_script(DENY_MICROPHONE_JS)
    ui.open(denied)
    denied.get_by_role("button", name="Start recording", exact=True).click()
    ui.expect_alert(re.compile(r"\S"), denied)
    expect(denied.get_by_role("textbox", name="Message")).to_be_editable()
    expect(denied.get_by_role("button", name="Attach WAV audio")).to_be_enabled()
    denied.close()
    assert provider.methods() == [] and server.session_bytes(sid) == baseline
    ui.cp("voice_success.microphone-denied-alert")

    # Starting a new intake during a take releases the microphone without STT.
    ui.button("Start recording").click()
    expect(ui.button("Stop recording")).to_be_visible()
    expect(ui.button("Stop recording").locator("svg[aria-hidden='true']")).to_have_count(1)
    with ui.page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/api/sessions")
    ) as pending:
        ui.button("New conversation").click()
    assert pending.value.status == 201
    expect(ui.button("Stop recording")).to_be_hidden()
    sid = ui.current_session_id()
    baseline = server.session_bytes(sid)
    transcript_before = ui.transcript_text()
    ui.cp("voice_success.new-intake-stops-recording")

    # Start/Stop recording on Chromium's fake media device -> accepted PCM WAV -> draft.
    ui.button("Start recording").click()
    expect(ui.button("Stop recording")).to_be_visible()
    ui.page.wait_for_function("window.__spy.contexts.some(c => c.currentTime > 0.5)")
    with ui.page.expect_response(lambda r: r.url.endswith("/api/transcriptions")) as pending:
        ui.button("Stop recording").click()
        recording_gate.wait_entered(timeout=2)
        expect(ui.message).to_have_js_property("readOnly", True)
        assert ui.page.locator("#support-chat button").evaluate_all(
            "buttons => buttons.length > 0 && buttons.every(button => button.disabled)"
        )
        assert len(provider.calls("transcribe")) == 1
        ui.cp("voice_success.stop-auto-transcribes-and-locks-controls")
        recording_gate.release()
    assert pending.value.status == 200
    expect(ui.message).to_have_value(STT_TEXT)
    take = provider.calls("transcribe")[0]
    assert_accepted_pcm_wav(take["audio"])
    assert server.session_bytes(sid) == baseline
    assert ui.transcript_text() == transcript_before
    ui.message.fill("")
    ui.cp("voice_success.recording-take-fills-draft")

    # Upload a valid finite WAV and transcribe: draft only, nothing committed.
    with ui.page.expect_response(lambda r: r.url.endswith("/api/transcriptions")) as pending:
        ui.upload(wav_path)
    assert pending.value.status == 200
    expect(ui.message).to_have_value(STT_TEXT)
    upload = ui.transcription_posts()[-1]
    assert upload["status"] == 200 and upload["response_body"]
    assert provider.calls("transcribe")[1]["audio"] == wav
    assert ui.transcript_text() == transcript_before
    assert server.session_bytes(sid) == baseline
    assert ui.turn_posts() == []
    saved = ui.disk(sid)
    assert saved["revision"] == 0 and len(saved["messages"]) == 1
    ui.cp("voice_success.upload-fills-draft-no-commit")

    # Edit the order in the draft, then Send: transcript mode and edited facts persist.
    ui.message.fill(EDITED)
    expect(ui.message).to_have_value(EDITED)
    assert ui.send(EDITED).status == 200
    sent = ui.turn_posts()[0]
    assert body_of(sent)["input_mode"] == "transcript" and body_of(sent)["text"] == EDITED
    expect(ui.review).to_be_visible()
    for line in SUMMARY.split("\n"):
        expect(ui.review).to_contain_text(line)
    saved = ui.disk(sid)
    user_message = [m for m in saved["messages"] if m["role"] == "user"][0]
    assert user_message["input_mode"] == "transcript" and user_message["text"] == EDITED
    assert saved["slots"]["order_number"] == "NS-000456"
    assert saved["revision"] == 1 and saved["pending_review_revision"] == 1
    ui.cp("voice_success.edited-transcript-persisted")

    # Play the saved reply: the saved message ID drives real, finite MP3 playback.
    reply = ui.last_assistant(sid)
    play_button = ui.page.get_by_role("button", name="Play reply 1", exact=True)
    expect(play_button).to_have_count(1)
    assert play_button.locator("svg[aria-hidden='true']").count() == 1
    assert play_button.inner_text() == ""
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        play_button.get_attribute("aria-label") or "",
    )
    before_play = server.session_bytes(sid)
    with ui.page.expect_response(lambda r: r.url.endswith("/speech")) as pending:
        ui.play_button(reply["id"]).click()
    assert pending.value.status == 200
    speech = ui.speech_posts()
    assert len(speech) == 1 and body_of(speech[0]) == {"message_id": reply["id"]}
    assert speech[0]["response_headers"]["content-type"] == "audio/mpeg"
    assert speech[0]["response_headers"]["cache-control"] == "no-store"
    assert speech[0]["response_bytes"] == len(mp3)
    assert provider.calls("synthesize")[0]["text"] == reply["text"] == review_reply(SUMMARY)
    expect(ui.audio).to_have_attribute("src", re.compile(r"^blob:"))
    ui.page.wait_for_function(PLAYBACK_READY)
    spy = ui.spy()
    assert [c["type"] for c in spy["created"]] == ["audio/mpeg"]
    assert spy["created"][0]["size"] == len(mp3)
    assert spy["plays"] and spy["plays"][-1] == spy["created"][0]["url"]
    expect(ui.page.get_by_text("Voice is AI-generated").first).to_be_visible()
    assert server.session_bytes(sid) == before_play
    ui.cp("voice_success.saved-reply-plays-mp3")

    # Second checkpoint: the same saved reply played from a PCM WAV response.
    wav_reply = make_wav(seconds=0.5, peak=1200)
    provider.script_synthesize(reply["text"], wav_reply, media_type="audio/wav")
    with ui.page.expect_response(lambda r: r.url.endswith("/speech")) as pending:
        ui.play_button(reply["id"]).click()
    assert pending.value.status == 200
    speech = ui.speech_posts()
    assert len(speech) == 2 and body_of(speech[1]) == {"message_id": reply["id"]}
    assert speech[1]["response_headers"]["content-type"] == "audio/wav"
    assert speech[1]["response_headers"]["cache-control"] == "no-store"
    assert speech[1]["response_bytes"] == len(wav_reply)
    expect(ui.audio).to_have_attribute("src", re.compile(r"^blob:"))
    src_wav = ui.audio.get_attribute("src")
    ui.page.wait_for_function(
        "(url) => { const a = document.querySelector('[data-testid=\"reply-audio\"]');"
        " return a.currentSrc === url && a.currentTime > 0 && a.duration > 0; }",
        arg=src_wav,
    )
    spy = ui.spy()
    assert [c["type"] for c in spy["created"]] == ["audio/mpeg", "audio/wav"]
    assert spy["created"][1]["size"] == len(wav_reply)
    assert spy["created"][1]["url"] == src_wav
    assert spy["created"][0]["url"] in spy["revoked"], "MP3 object URL replaced by WAV"
    assert server.session_bytes(sid) == before_play
    ui.cp("voice_success.saved-reply-plays-wav")

    # Confirmation still succeeds after voice input and playback.
    assert ui.confirm().status == 200
    expect(ui.log).to_contain_text(RECEIPT_TEMPLATE.format(session_id=sid))
    saved = ui.disk(sid)
    assert saved["status"] == "completed" and saved["revision"] == 2
    assert saved["confirmation"]["review_revision"] == 1
    assert provider.methods() == [
        "transcribe", "transcribe", "extract", "synthesize", "synthesize",
    ]  # fmt: skip
    provider.assert_consumed()
    ui.cp("voice_success.confirm-after-voice")
