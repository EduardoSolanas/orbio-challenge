import { afterEach, beforeEach, describe, expect, jest, test } from "@jest/globals";

import { MAX_RECORD_SECONDS, MAX_WAV_BYTES } from "../../web/js/config.js";
import { MicrophoneRecording } from "../../web/js/microphone.js";
import { WavEncoder } from "../../web/js/recording.js";
import {
  EN,
  FakeResponse,
  ScriptedHttp,
  SESSION_ID,
  audio,
  deferred,
  failure,
  flush,
  ok,
  sessionView,
  startApp,
  turnResponse,
} from "./support/page.js";

const SESSIONS = "/api/sessions";
const SPEECH = `/api/sessions/${SESSION_ID}/speech`;
const TRANSCRIBE = "/api/transcriptions";
const REPLY_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

async function running() {
  const http = new ScriptedHttp().on("POST", SESSIONS, ok(sessionView()));
  const app = await startApp({ http });
  return { app, http };
}

async function withReply() {
  const { app, http } = await running();
  await app.presenter.applyTurnResponse(turnResponse({ message_id: REPLY_ID }), "hi", "text");
  return { app, http };
}

describe("playing a reply", () => {
  test("fetches the audio, loads it into the player and plays it", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());

    await app.voice.playReply(REPLY_ID);

    expect(JSON.parse(http.to("POST", SPEECH)[0].options.body)).toEqual({ message_id: REPLY_ID });
    expect(app.state.objectUrl).toBe("blob:reply");
    expect(app.dom.audio.getAttribute("src")).toBe("blob:reply");
    expect(window.HTMLMediaElement.prototype.play).toHaveBeenCalled();
    expect(app.state.speechPending).toBeNull();
  });

  test("clicking the speaker button plays that reply", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());

    app.dom.log.querySelector(`button[data-message-id="${REPLY_ID}"]`).click();
    await flush();
    await flush();

    expect(http.to("POST", SPEECH)).toHaveLength(1);
  });

  test("speaker toggles to stop, stops without another request, and can replay", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    const button = app.dom.log.querySelector(`button[data-message-id="${REPLY_ID}"]`);
    const speaker = button.querySelector("path").getAttribute("d");
    button.click();
    await flush();
    await flush();
    expect(button.getAttribute("aria-label")).toBe("Stop reply 2");
    expect(button.querySelector("path").getAttribute("d")).not.toBe(speaker);
    button.click();
    expect(window.HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    expect(app.dom.audio.hasAttribute("src")).toBe(false);
    expect(button.getAttribute("aria-label")).toBe("Play reply 2");
    expect(button.querySelector("path").getAttribute("d")).toBe(speaker);
    expect(http.to("POST", SPEECH)).toHaveLength(1);
    button.click();
    await flush();
    await flush();
    expect(http.to("POST", SPEECH)).toHaveLength(2);
  });

  test.each(["ended", "error", "pause"])("%s restores the speaker", async (event) => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    await app.voice.playReply(REPLY_ID);
    const button = app.dom.log.querySelector(`button[data-message-id="${REPLY_ID}"]`);
    expect(button.getAttribute("aria-label")).toBe("Stop reply 2");
    app.dom.audio.dispatchEvent(new Event(event));
    expect(button.getAttribute("aria-label")).toBe("Play reply 2");
  });

  test("stop remains usable while the browser play promise is pending", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    const playback = deferred();
    window.HTMLMediaElement.prototype.play.mockReturnValueOnce(playback.promise);
    const playing = app.voice.playReply(REPLY_ID);
    await flush();
    const button = app.dom.log.querySelector(`button[data-message-id="${REPLY_ID}"]`);
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-label")).toBe("Stop reply 2");
    button.click();
    playback.reject(new Error("interrupted"));
    await playing;
    expect(button.getAttribute("aria-label")).toBe("Play reply 2");
    expect(app.dom.alert.hidden).toBe(true);
  });

  test("switching replies restores the previous speaker, including after redraw", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    await app.voice.playReply(REPLY_ID);
    await app.voice.playReply("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    app.view.renderTranscript();
    const buttons = app.dom.log.querySelectorAll("button[data-message-id]");
    expect(buttons[0].getAttribute("aria-label")).toBe("Stop reply 1");
    expect(buttons[1].getAttribute("aria-label")).toBe("Play reply 2");
  });

  test("failed playback restores the speaker and Spanish uses a translated stop label", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    await app.i18n.load("es");
    app.state.language = "es";
    await app.voice.playReply(REPLY_ID);
    const button = app.dom.log.querySelector(`button[data-message-id="${REPLY_ID}"]`);
    expect(button.getAttribute("aria-label")).toBe("Detener respuesta 2");
    button.click();
    window.HTMLMediaElement.prototype.play.mockRejectedValueOnce(new Error("blocked"));
    await app.voice.playReply(REPLY_ID);
    expect(button.getAttribute("aria-label")).toBe("Reproducir respuesta 2");
  });

  test("marks the reply as loading while it is fetched and ignores a second click on it", async () => {
    const { app, http } = await withReply();
    const response = deferred();
    http.on("POST", SPEECH, () => response.promise);

    const first = app.voice.playReply(REPLY_ID);
    await flush();
    const button = app.dom.log.querySelector(`button[data-message-id="${REPLY_ID}"]`);
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("aria-label")).toBe(EN["speech.loading"]);
    await app.voice.playReply(REPLY_ID);
    expect(http.to("POST", SPEECH)).toHaveLength(1);

    response.resolve(audio());
    await first;

    expect(button.disabled).toBe(false);
  });

  test("replaces the previous audio when a new one is played", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    await app.voice.playReply(REPLY_ID);

    await app.voice.playReply("other-reply");

    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith("blob:reply");
  });

  test("a slower earlier reply never plays over a newer one", async () => {
    const { app, http } = await withReply();
    const slow = deferred();
    http.on("POST", SPEECH, () => slow.promise, audio());

    const first = app.voice.playReply("first");
    await flush();
    const second = app.voice.playReply("second");
    await second;
    slow.resolve(audio());
    await first;

    expect(window.HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  });

  test("a refusal shows its message", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, failure("provider_configuration", 503));

    await app.voice.playReply(REPLY_ID);

    expect(app.dom.alert.hidden).toBe(false);
    expect(window.HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  test.each([
    ["an empty recording", audio(0)],
    ["a file that is not audio", audio(8, "text/html")],
  ])("%s shows the playback fallback message", async (_label, response) => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, response);

    await app.voice.playReply(REPLY_ID);

    expect(app.dom.alert.textContent).toBe(EN["ui.playback_fallback"]);
    expect(window.URL.createObjectURL).not.toHaveBeenCalled();
  });

  test("a browser that refuses to play shows the fallback message", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());
    window.HTMLMediaElement.prototype.play.mockRejectedValue(new Error("blocked"));

    await app.voice.playReply(REPLY_ID);

    expect(app.dom.alert.textContent).toBe(EN["ui.playback_fallback"]);
  });

  test("a network failure alerts; a timeout says so", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, new Error("offline"));
    await app.voice.playReply(REPLY_ID);
    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);

    http.on("POST", SPEECH, Object.assign(new Error("slow"), { name: "AbortError" }));
    await app.voice.playReply(REPLY_ID);
    expect(app.dom.alert.textContent).toBe(EN["errors.provider_timeout"]);
  });

  test("does nothing without a session", async () => {
    const app = await startApp({ http: new ScriptedHttp().on("POST", SESSIONS, new Error("offline")) });

    await app.voice.playReply(REPLY_ID);

    expect(app.state.speechPending).toBeNull();
  });

  test("a reply that arrives after a new conversation started is dropped", async () => {
    const { app, http } = await withReply();
    const response = deferred();
    http.on("POST", SPEECH, () => response.promise);

    const playing = app.voice.playReply(REPLY_ID);
    await flush();
    app.state.generation += 1;
    response.resolve(audio());
    await playing;

    expect(window.HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  test("audio that goes stale while it is being prepared is released, not played", async () => {
    const { app, http } = await withReply();
    http.on("POST", SPEECH, audio());

    // A newer playback supersedes this one while the blob is read.
    class SlowBlob extends FakeResponse {
      async blob() {
        app.state.bumpPlayback();
        return this.blobValue;
      }
    }
    http.on("POST", SPEECH, new SlowBlob({}, { blob: { size: 8, type: "audio/mpeg" } }));
    await app.voice.playReply(REPLY_ID);
    expect(window.URL.createObjectURL).not.toHaveBeenCalled();

    // ...while the object URL is created.
    http.on("POST", SPEECH, audio());
    window.URL.createObjectURL.mockImplementationOnce(() => {
      app.state.bumpPlayback();
      return "blob:stale-1";
    });
    await app.voice.playReply(REPLY_ID);
    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith("blob:stale-1");

    // ...while the previous audio is released.
    window.URL.createObjectURL.mockImplementationOnce(() => "blob:stale-2");
    window.HTMLMediaElement.prototype.load.mockImplementationOnce(() => app.state.bumpPlayback());
    await app.voice.playReply(REPLY_ID);
    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith("blob:stale-2");
    expect(window.HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });
});

describe("transcription", () => {
  const wavFile = () => new File(["RIFF"], "spoken.wav", { type: "audio/wav" });

  function choose(app, file) {
    Object.defineProperty(app.dom.wav, "files", { value: [file], configurable: true });
    app.voice.onWavSelected();
  }

  test("an uploaded file is transcribed into an editable draft", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, ok({ text: "my meter is broken" }));

    choose(app, wavFile());
    await flush();
    await flush();

    expect(http.to("POST", TRANSCRIBE)[0].options.body.get("file").name).toBe("spoken.wav");
    expect(app.dom.message.value).toBe("my meter is broken");
    expect(app.state.inputMode).toBe("transcript");
    expect(app.state.audioBlob).toBeNull();
    expect(app.state.sttPending).toBe(false);
    expect(app.state.sttAbort).toBeNull();
  });

  test("a new file replaces the transcript already in the box", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, ok({ text: "first take" }));
    choose(app, wavFile());
    await flush();
    await flush();
    expect(app.dom.message.value).toBe("first take");

    http.on("POST", TRANSCRIBE, ok({ text: "second take" }));
    choose(app, wavFile());
    await flush();
    await flush();

    expect(http.to("POST", TRANSCRIBE)).toHaveLength(2);
    expect(app.dom.message.value).toBe("second take");
    expect(app.state.inputMode).toBe("transcript");
  });

  test("the panel is locked while the file is being transcribed", async () => {
    const { app, http } = await running();
    const response = deferred();
    http.on("POST", TRANSCRIBE, () => response.promise);

    choose(app, wavFile());
    await flush();

    expect(app.dom.chatPanel.getAttribute("aria-busy")).toBe("true");
    expect(app.dom.help.hidden).toBe(false);

    response.resolve(ok({ text: "done" }));
    await flush();
    await flush();

    expect(app.dom.chatPanel.hasAttribute("aria-busy")).toBe(false);
  });

  test("a body without text leaves an empty draft", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, ok({}));

    choose(app, wavFile());
    await flush();
    await flush();

    expect(app.dom.message.value).toBe("");
  });

  test("a refusal shows its message and drops the audio", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, failure("silent_audio", 422));

    choose(app, wavFile());
    await flush();
    await flush();

    expect(app.dom.alert.textContent).toBe(EN["errors.silent_audio"]);
    expect(app.state.audioBlob).toBeNull();
  });

  test.each([
    ["a timeout", Object.assign(new Error("slow"), { name: "AbortError" }), "errors.provider_timeout"],
    ["a network failure", new Error("offline"), "errors.provider_unavailable"],
  ])("%s shows the matching message", async (_label, error, key) => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, error);

    choose(app, wavFile());
    await flush();
    await flush();

    expect(app.dom.alert.textContent).toBe(EN[key]);
    expect(app.state.audioBlob).toBeNull();
  });

  test("only one transcription runs at a time", async () => {
    const { app, http } = await running();
    const response = deferred();
    http.on("POST", TRANSCRIBE, () => response.promise);
    choose(app, wavFile());
    await flush();

    app.state.sttPending = false;
    app.state.audioBlob = wavFile();
    await app.voice.onTranscribe();

    expect(http.to("POST", TRANSCRIBE)).toHaveLength(1);
    response.resolve(ok({ text: "x" }));
    await flush();
  });

  test("does nothing when a draft is already typed or there is no audio", async () => {
    const { app, http } = await running();

    await app.voice.onTranscribe();
    app.state.audioBlob = wavFile();
    app.dom.message.value = "typed";
    await app.voice.onTranscribe();

    expect(http.to("POST", TRANSCRIBE)).toHaveLength(0);
  });

  test("no file chosen clears the selection without transcribing", async () => {
    const { app, http } = await running();
    Object.defineProperty(app.dom.wav, "files", { value: [], configurable: true });

    app.voice.onWavSelected();

    expect(app.state.audioBlob).toBeNull();
    expect(http.to("POST", TRANSCRIBE)).toHaveLength(0);
  });

  test("a result for an abandoned conversation is dropped", async () => {
    const { app, http } = await running();
    const response = deferred();
    http.on("POST", TRANSCRIBE, () => response.promise);

    choose(app, wavFile());
    await flush();
    app.state.generation += 1;
    response.resolve(ok({ text: "late" }));
    await flush();
    await flush();

    expect(app.dom.message.value).toBe("");
  });

  test("a refusal that arrives after a new conversation is dropped too", async () => {
    const { app, http } = await running();
    const response = deferred();
    http.on("POST", TRANSCRIBE, () => response.promise);

    choose(app, wavFile());
    await flush();
    app.state.generation += 1;
    response.resolve(failure("silent_audio", 422));
    await flush();
    await flush();

    expect(app.dom.alert.hidden).toBe(true);
  });
});

describe("recording", () => {
  let stream;
  let track;
  let contexts;
  let getUserMedia;

  function installMicrophone({ state = "running" } = {}) {
    contexts = [];
    track = { stop: jest.fn() };
    stream = { getTracks: () => [track] };
    getUserMedia = jest.fn(async () => stream);
    window.AudioContext = class {
      constructor() {
        this.sampleRate = 16000;
        this.state = state;
        this.destination = {};
        this.closed = false;
        contexts.push(this);
      }

      resume() {
        this.state = "running";
        return Promise.resolve();
      }

      createMediaStreamSource() {
        return { connect: jest.fn(), disconnect: jest.fn() };
      }

      createScriptProcessor() {
        this.processor = { connect: jest.fn(), disconnect: jest.fn(), onaudioprocess: null };
        return this.processor;
      }

      close() {
        this.closed = true;
      }
    };
    Object.defineProperty(window.navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  }

  const feed = (samples) =>
    contexts.at(-1).processor.onaudioprocess({ inputBuffer: { getChannelData: () => samples } });
  const speech = () => Float32Array.from({ length: 1600 }, () => 0.5);

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
    delete window.AudioContext;
    Object.defineProperty(window.navigator, "mediaDevices", { configurable: true, value: undefined });
  });

  test("a browser without a microphone API says the microphone is unavailable", async () => {
    const { app } = await running();
    Object.defineProperty(window.navigator, "mediaDevices", { configurable: true, value: undefined });

    await app.voice.startRecording();

    expect(app.dom.alert.textContent).toBe(EN["ui.mic_denied"]);
    expect(app.state.recording).toBeNull();
  });

  test("a refused permission says so and leaves the page usable", async () => {
    const { app } = await running();
    installMicrophone();
    getUserMedia.mockRejectedValue(new Error("denied"));

    await app.voice.startRecording();

    expect(app.dom.alert.textContent).toBe(EN["ui.mic_denied"]);
    expect(app.state.recordingPending).toBe(false);
    expect(app.state.recording).toBeNull();
  });

  test("starting shows the stop button; starting again while recording does nothing", async () => {
    const { app } = await running();
    installMicrophone();

    await app.voice.startRecording();
    await app.voice.startRecording();

    expect(app.state.recording).toBeInstanceOf(MicrophoneRecording);
    expect(app.dom.stopRecording.hidden).toBe(false);
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
  });

  test("a suspended audio context is resumed first", async () => {
    const { app } = await running();
    installMicrophone({ state: "suspended" });

    await app.voice.startRecording();

    expect(contexts[0].state).toBe("running");
    expect(app.state.recording).not.toBeNull();
  });

  test("stopping turns what was said into a WAV and transcribes it", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, ok({ text: "order NS-000123 arrived broken" }));
    installMicrophone();
    await app.voice.startRecording();
    feed(speech());

    app.voice.stopRecording();
    await flush();
    await flush();

    expect(track.stop).toHaveBeenCalled();
    expect(contexts[0].closed).toBe(true);
    expect(app.state.recording).toBeNull();
    expect(app.dom.stopRecording.hidden).toBe(true);
    expect(http.to("POST", TRANSCRIBE)[0].options.body.get("file").name).toBe("recording.wav");
    expect(app.dom.message.value).toBe("order NS-000123 arrived broken");
  });

  test("a take with no signal yet is given a short grace period before it is judged", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, ok({ text: "hello" }));
    installMicrophone();
    await app.voice.startRecording();
    jest.useFakeTimers();

    app.voice.stopRecording();
    expect(app.state.recording).not.toBeNull();

    await jest.advanceTimersByTimeAsync(250);

    expect(app.state.recording).toBeNull();
    expect(track.stop).toHaveBeenCalled();
  });

  test("a forced stop does not wait", async () => {
    const { app } = await running();
    installMicrophone();
    await app.voice.startRecording();

    app.voice.stopRecording({ force: true });

    expect(app.state.recording).toBeNull();
  });

  test("reaching the length limit stops the recording", async () => {
    const { app, http } = await running();
    http.on("POST", TRANSCRIBE, ok({ text: "long" }));
    installMicrophone();
    await app.voice.startRecording();

    feed(new Float32Array(16000 * MAX_RECORD_SECONDS));
    await flush();

    expect(app.state.recording).toBeNull();
    expect(http.to("POST", TRANSCRIBE)).toHaveLength(1);
  });

  test("the time limit stops a recording even if no audio arrives", async () => {
    const { app } = await running();
    installMicrophone();
    jest.useFakeTimers();
    await app.voice.startRecording();

    await jest.advanceTimersByTimeAsync(MAX_RECORD_SECONDS * 1000);

    expect(app.state.recording).toBeNull();
  });

  test("audio that arrives after the recording stopped is ignored", async () => {
    const { app } = await running();
    installMicrophone();
    await app.voice.startRecording();
    const accumulator = app.state.recording.accumulator;
    app.state.recording.stopped = true;

    feed(speech());

    expect(accumulator.frameCount).toBe(0);
  });

  test("discarding releases the microphone and the audio context", async () => {
    const { app } = await running();
    installMicrophone();
    await app.voice.startRecording();

    app.voice.discardRecording();

    expect(app.state.recording).toBeNull();
    expect(track.stop).toHaveBeenCalled();
    expect(contexts[0].closed).toBe(true);
  });

  test("discarding with no recording does nothing", async () => {
    const { app } = await running();

    expect(() => app.voice.discardRecording()).not.toThrow();
  });

  test("stopping with no recording, or one that already stopped, does nothing", async () => {
    const { app } = await running();
    installMicrophone();

    app.voice.stopRecording();
    await app.voice.startRecording();
    app.state.recording.stopped = true;
    app.voice.stopRecording();

    expect(app.state.recording).not.toBeNull();
  });

  test("a recording that is too large is rejected without transcribing", async () => {
    const { app, http } = await running();
    installMicrophone();
    jest.spyOn(WavEncoder, "encode").mockReturnValue({ size: MAX_WAV_BYTES + 1 });
    await app.voice.startRecording();
    feed(speech());

    app.voice.stopRecording();

    expect(app.dom.alert.textContent).toBe(EN["errors.invalid_audio"]);
    expect(app.state.audioBlob).toBeNull();
    expect(http.to("POST", TRANSCRIBE)).toHaveLength(0);
  });

  test("a new conversation while permission is pending releases the microphone", async () => {
    const { app } = await running();
    installMicrophone();
    const permission = deferred();
    getUserMedia.mockReturnValue(permission.promise);

    const starting = app.voice.startRecording();
    app.state.generation += 1;
    permission.resolve(stream);
    await starting;

    expect(track.stop).toHaveBeenCalled();
    expect(app.state.recording).toBeNull();
    expect(contexts).toHaveLength(0);
  });

  test("a new conversation while the audio context starts releases both", async () => {
    const { app } = await running();
    installMicrophone({ state: "suspended" });
    const resumed = deferred();
    const original = window.AudioContext;
    window.AudioContext = class extends original {
      resume() {
        return resumed.promise;
      }
    };

    const starting = app.voice.startRecording();
    await flush();
    app.state.generation += 1;
    resumed.resolve();
    await starting;

    expect(track.stop).toHaveBeenCalled();
    expect(contexts[0].closed).toBe(true);
    expect(app.state.recording).toBeNull();
  });

  test("a transcript in the box does not block a new recording", async () => {
    const { app } = await running();
    installMicrophone();
    app.dom.message.value = "dictated text";
    app.state.inputMode = "transcript";

    await app.voice.startRecording();

    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(app.state.recording).not.toBeNull();
  });

  test("typing a draft or an ongoing transcription blocks recording", async () => {
    const { app } = await running();
    installMicrophone();

    app.dom.message.value = "typed";
    await app.voice.startRecording();
    app.dom.message.value = "";
    app.state.sttPending = true;
    await app.voice.startRecording();

    expect(getUserMedia).not.toHaveBeenCalled();
  });
});
