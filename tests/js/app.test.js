import { describe, expect, jest, test } from "@jest/globals";

import { App } from "../../app/static/js/app.js";
import {
  EN,
  ES,
  ScriptedHttp,
  SESSION_ID,
  failure,
  flush,
  ok,
  sessionView,
  startApp,
  turnResponse,
} from "./support/page.js";

const SESSIONS = "/api/sessions";
const SESSION = `/api/sessions/${SESSION_ID}`;
const TURNS = `${SESSION}/turns`;

async function running() {
  const http = new ScriptedHttp().on("POST", SESSIONS, ok(sessionView()));
  const app = await startApp({ http });
  return { app, http };
}

function type(app, text) {
  app.dom.message.value = text;
  app.dom.message.dispatchEvent(new Event("input", { bubbles: true }));
}

function press(app, init) {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  app.dom.message.dispatchEvent(event);
  return event;
}

describe("starting", () => {
  test("shows the English page, creates a session and enables the controls", async () => {
    const { app } = await running();

    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toBe("");
    expect(document.querySelector("[data-i18n='site.chat_status']").textContent).toBe(EN["site.chat_status"]);
    expect(app.dom.send.getAttribute("aria-label")).toBe(EN["actions.send"]);
    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.dom.startRecording.disabled).toBe(false);
  });

  test("the page cannot be started without its bootstrap catalogue", () => {
    const app = new App({ window, http: new ScriptedHttp() });
    document.getElementById("bootstrap-locale")?.remove();

    expect(() => app.start()).toThrow(/bootstrap locale/i);
  });

  test("uses the browser's own request id when none is injected", () => {
    // jsdom has no crypto.randomUUID, so the browser's is stood in for.
    const randomUUID = jest.fn(() => "44444444-4444-4444-8444-444444444444");
    Object.defineProperty(window.crypto, "randomUUID", { value: randomUUID, configurable: true });
    const app = new App({ window, http: new ScriptedHttp() });

    expect(app.turns.newRequestId()).toBe("44444444-4444-4444-8444-444444444444");
    expect(randomUUID).toHaveBeenCalledTimes(1);
    delete window.crypto.randomUUID;
  });

  test("builds a real HTTP layer when none is injected", () => {
    const app = new App({ window });

    expect(app.api.http.fetchWithDeadline).toBeInstanceOf(Function);
  });
});

describe("sending", () => {
  test("Enter sends the message", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse()));
    type(app, "Hello");

    const event = press(app, { key: "Enter" });
    await flush();
    await flush();

    expect(event.defaultPrevented).toBe(true);
    expect(http.to("POST", TURNS)).toHaveLength(1);
    expect(app.dom.log.textContent).toContain("Hello");
  });

  test.each([
    ["Shift+Enter", { key: "Enter", shiftKey: true }],
    ["another key", { key: "a" }],
    ["Enter while composing text", { key: "Enter", isComposing: true }],
  ])("%s does not send", async (_label, init) => {
    const { app, http } = await running();
    type(app, "Hello");

    const event = press(app, init);
    await flush();

    expect(event.defaultPrevented).toBe(false);
    expect(http.to("POST", TURNS)).toHaveLength(0);
  });

  test("the send button sends the message", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse()));
    type(app, "Hello");

    app.dom.send.click();
    await flush();
    await flush();

    expect(http.to("POST", TURNS)).toHaveLength(1);
  });

  test("typing keeps send disabled until there is text", async () => {
    const { app } = await running();
    expect(app.dom.send.disabled).toBe(true);

    type(app, "Hello");
    expect(app.dom.send.disabled).toBe(false);

    type(app, "");
    expect(app.dom.send.disabled).toBe(true);
  });

  test("editing a message that failed to send cancels its retry payload", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, new Error("offline"));
    type(app, "Hello");
    await app.turns.onSend();
    expect(app.state.retryKind).toBe("turn");

    type(app, "Hello again");

    expect(app.state.retryKind).toBeNull();
  });

  test("clearing a dictated draft returns to typing mode", async () => {
    const { app } = await running();
    app.state.inputMode = "transcript";
    app.dom.message.value = "dictated";
    app.dom.message.dispatchEvent(new Event("input"));
    expect(app.state.inputMode).toBe("transcript");

    type(app, "");

    expect(app.state.inputMode).toBe("text");
  });
});

describe("buttons", () => {
  test("New conversation starts another session", async () => {
    const { app, http } = await running();
    http.on("POST", SESSIONS, ok(sessionView({ session_id: "33333333-3333-4333-8333-333333333333" })));

    app.dom.newIntake.click();
    await flush();
    await flush();

    expect(app.state.sessionId).toBe("33333333-3333-4333-8333-333333333333");
  });

  test("Confirm confirms the review", async () => {
    const { app, http } = await running();
    await app.presenter.applyTurnResponse(
      turnResponse({ status: "awaiting_confirmation", pending_review_revision: 1, summary: "S", revision: 1 }),
      "details",
      "text"
    );
    http.on("POST", `${SESSION}/confirm`, ok(turnResponse({ status: "completed", revision: 2 })));
    http.on("GET", SESSION, ok(sessionView({ status: "completed", revision: 2 })));

    app.dom.confirm.click();
    await flush();
    await flush();

    expect(app.state.status).toBe("completed");
  });

  test("Retry repeats what failed", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, failure("storage_unavailable", 503), ok(sessionView()));
    const app = await startApp({ http });
    expect(app.dom.retry.hidden).toBe(false);

    app.dom.retry.click();
    await flush();
    await flush();

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.dom.retry.hidden).toBe(true);
  });

  test("Verify submits the form", async () => {
    const { app, http } = await running();
    await app.presenter.applyTurnResponse(turnResponse({ order_verification_required: true }), "status", "text");
    http.on("POST", `${SESSION}/order-verification`, ok({ verified: true, error: null }));
    app.dom.postcode.value = "SW1A 1AA";
    app.dom.postcode.dispatchEvent(new Event("input"));
    expect(app.dom.verify.disabled).toBe(true);
    app.dom.code.value = "246810";
    app.dom.code.dispatchEvent(new Event("input"));
    expect(app.dom.verify.disabled).toBe(false);

    app.dom.verify.click();
    await flush();
    await flush();

    expect(app.dom.verification.hidden).toBe(true);
  });

  test("the attach button opens the file chooser only while it is enabled", async () => {
    const { app } = await running();
    const clicked = jest.spyOn(app.dom.wav, "click").mockImplementation(() => {});

    app.dom.wavAttach.click();
    expect(clicked).toHaveBeenCalledTimes(1);

    app.state.pending = true;
    app.view.updateControls();
    app.dom.wavAttach.disabled = true;
    app.dom.wavAttach.dispatchEvent(new MouseEvent("click"));
    expect(clicked).toHaveBeenCalledTimes(1);
  });

  test("choosing a file starts a transcription", async () => {
    const { app, http } = await running();
    http.on("POST", "/api/transcriptions", ok({ text: "from a file" }));
    Object.defineProperty(app.dom.wav, "files", {
      value: [new File(["RIFF"], "a.wav", { type: "audio/wav" })],
      configurable: true,
    });

    app.dom.wav.dispatchEvent(new Event("change"));
    await flush();
    await flush();

    expect(app.dom.message.value).toBe("from a file");
  });

  test("the record buttons call the voice controller", async () => {
    const { app } = await running();
    app.voice.startRecording = jest.fn();
    app.voice.stopRecording = jest.fn();

    app.dom.startRecording.click();
    app.dom.stopRecording.hidden = false;
    app.dom.stopRecording.disabled = false;
    app.dom.stopRecording.click();

    expect(app.voice.startRecording).toHaveBeenCalled();
    expect(app.voice.stopRecording).toHaveBeenCalled();
  });
});

describe("leaving the page", () => {
  test("pagehide abandons the conversation's pending work", async () => {
    const { app } = await running();
    const discard = jest.fn();
    app.state.recording = { discard };
    app.state.speechPending = { generation: 0, messageId: "m", token: 1 };
    app.state.objectUrl = "blob:reply";
    const generation = app.state.generation;

    window.dispatchEvent(new Event("pagehide"));

    expect(app.state.generation).toBe(generation + 1);
    expect(app.state.speechPending).toBeNull();
    expect(discard).toHaveBeenCalled();
    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith("blob:reply");
  });
});

describe("language", () => {
  test("a Spanish reply switches the page to Spanish", async () => {
    const { app, http } = await running();
    app.i18n.seed("es", ES);
    http.on("POST", TURNS, ok(turnResponse({ reply_language: "es", reply: "Hola" })));
    type(app, "Hola");

    await app.turns.onSend();

    expect(document.documentElement.lang).toBe("es");
    expect(app.dom.send.getAttribute("aria-label")).toBe(ES["actions.send"]);
  });
});
