import { describe, expect, test } from "@jest/globals";

import {
  EN,
  ES,
  ScriptedHttp,
  SECOND_SESSION_ID,
  SESSION_ID,
  deferred,
  failure,
  ok,
  sessionView,
  startApp,
  turnResponse,
} from "./support/page.js";

const SESSIONS = "/api/sessions";
const TURNS = `/api/sessions/${SESSION_ID}/turns`;
const NEW_TURNS = `/api/sessions/${SECOND_SESSION_ID}/turns`;

async function running(view = sessionView()) {
  const http = new ScriptedHttp().on("POST", SESSIONS, ok(view));
  const app = await startApp({ http });
  return { app, http };
}

const labels = (app) => [...app.dom.starters.querySelectorAll("button")].map((b) => b.textContent);
const click = (app, key) => app.dom.starters.querySelector(`[data-starter="${key}"]`).click();
const bodyOf = (http) => JSON.parse(http.to("POST", TURNS)[0].options.body);

describe("showing the starter buttons", () => {
  test("a fresh conversation offers the four options before the customer types", async () => {
    const { app } = await running();

    expect(app.dom.starters.hidden).toBe(false);
    expect(labels(app)).toEqual([
      EN["starters.report"],
      EN["starters.question"],
      EN["starters.installation"],
      EN["starters.booking"],
    ]);
    expect(app.dom.starters.getAttribute("aria-label")).toBe(EN["starters.region"]);
  });

  test("the labels follow the conversation language", async () => {
    const { app } = await running();
    await app.i18n.load("es");

    app.state.language = "es";
    app.view.renderStarters();

    expect(labels(app)[0]).toBe(ES["starters.report"]);
    expect(labels(app)).toHaveLength(4);
  });

  test("they come back under the replies that list what the assistant can do", async () => {
    for (const reply of [EN.capability_reply, EN.capability]) {
      const { app } = await running();
      await app.presenter.applyTurnResponse(turnResponse({ reply }), "help me", "text");

      expect(app.dom.starters.hidden).toBe(false);
    }
  });

  test("they stay away after any other reply", async () => {
    const { app } = await running();

    await app.presenter.applyTurnResponse(turnResponse({ reply: "What is your account reference?" }), "hi", "text");

    expect(app.dom.starters.hidden).toBe(true);
    expect(app.dom.starters.children).toHaveLength(0);
  });

  test.each([
    ["the conversation was handed to a person", (s) => (s.status = "handed_off")],
    ["the account must be verified", (s) => (s.orderVerificationRequired = true)],
    ["times are offered", (s) => (s.slotOptions = [{ starts_at: "2026-11-02T09:00", ends_at: "2026-11-02T11:00" }])],
    ["a review awaits confirmation", (s) => Object.assign(s, { status: "awaiting_confirmation", pendingReviewRevision: 1, summary: "x" })],
    ["a reply is being waited for", (s) => (s.turnPending = true)],
  ])("they are hidden while %s", async (_name, change) => {
    const { app } = await running();
    change(app.state);

    app.view.renderStarters();

    expect(app.dom.starters.hidden).toBe(true);
  });

  test("they are disabled while the page is busy", async () => {
    const { app } = await running();

    app.state.pending = true;
    app.view.updateControls();

    expect([...app.dom.starters.querySelectorAll("button")].every((b) => b.disabled)).toBe(true);
  });
});

describe("pressing a starter button", () => {
  test("report sends the ready-made message through the normal turn and the buttons go away", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse({ reply: "What is your account reference?", revision: 1 })));

    click(app, "report");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(bodyOf(http)).toMatchObject({
      text: EN["starters.report_message"],
      expected_revision: 0,
      input_mode: "text",
    });
    expect(app.state.messages.map((m) => m.role)).toEqual(["assistant", "user", "assistant"]);
    expect(app.dom.starters.hidden).toBe(true);
  });

  test("installation and booking send their own messages", async () => {
    for (const [key, text] of [
      ["installation", EN["starters.installation_message"]],
      ["booking", EN["starters.booking_message"]],
    ]) {
      const { app, http } = await running();
      http.on("POST", TURNS, ok(turnResponse({ revision: 1 })));

      await app.turns.onStarter(key);

      expect(bodyOf(http).text).toBe(text);
    }
  });

  test("the message is sent in the conversation language", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse({ revision: 1 })));
    await app.i18n.load("es");
    app.state.language = "es";

    await app.turns.onStarter("report");

    expect(bodyOf(http).text).toBe(ES["starters.report_message"]);
  });

  test("hide the buttons at once while the reply is awaited", async () => {
    const { app, http } = await running();
    const reply = deferred();
    http.on("POST", TURNS, () => reply.promise);

    const sending = app.turns.onStarter("report");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(app.dom.starters.hidden).toBe(true);
    reply.resolve(ok(turnResponse({ revision: 1 })));
    await sending;
  });

  test("the question button only moves the cursor to the message box", async () => {
    const { app, http } = await running();

    click(app, "question");

    expect(window.document.activeElement).toBe(app.dom.message);
    expect(http.to("POST", TURNS)).toHaveLength(0);
    expect(app.dom.starters.hidden).toBe(false);
  });

  test("nothing is sent without a session, while busy, or after a hand-off", async () => {
    const { app, http } = await running();

    app.state.pending = true;
    await app.turns.onStarter("report");
    app.state.pending = false;
    app.state.status = "handed_off";
    await app.turns.onStarter("report");
    app.state.status = "collecting";
    app.state.sessionId = null;
    await app.turns.onStarter("report");

    expect(http.to("POST", TURNS)).toHaveLength(0);
    expect(http.to("POST", SESSIONS)).toHaveLength(1);
  });
});

describe("clearing local storage from the test card", () => {
  test("empties the browser storage and opens a new conversation", async () => {
    const { app, http } = await running();
    app.window.localStorage.setItem("something-else", "x");
    http.on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })));

    await app.onClearStorage();

    expect(app.window.localStorage.getItem("something-else")).toBeNull();
    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
    expect(app.store.load()).toBe(SECOND_SESSION_ID);
  });

  test("still starts a new conversation when the browser refuses the clear", async () => {
    const { app, http } = await running();
    http.on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })));
    app.window.localStorage.clear = () => {
      throw new Error("blocked");
    };

    await app.onClearStorage();

    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
  });
});

describe("after a saved request", () => {
  async function completed() {
    const { app, http } = await running();
    await app.presenter.applyTurnResponse(
      turnResponse({ status: "completed", reply: "Saved. Is there anything else I can help with?", revision: 3 }),
      "confirm",
      "text"
    );
    return { app, http };
  }

  test("the four options are offered again and can be pressed", async () => {
    const { app } = await completed();

    expect(app.dom.starters.hidden).toBe(false);
    expect(labels(app)).toHaveLength(4);
    expect([...app.dom.starters.querySelectorAll("button")].every((b) => !b.disabled)).toBe(true);
  });

  test("pressing one opens a new conversation and sends that option there", async () => {
    const { app, http } = await completed();
    http
      .on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })))
      .on("POST", NEW_TURNS, ok(turnResponse({ session_id: SECOND_SESSION_ID, revision: 1 })));

    await app.turns.onStarter("booking");

    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
    expect(http.to("POST", SESSIONS)).toHaveLength(2);
    expect(JSON.parse(http.to("POST", NEW_TURNS)[0].options.body)).toMatchObject({
      text: EN["starters.booking_message"],
      expected_revision: 0,
    });
    expect(http.to("POST", TURNS)).toHaveLength(0);
  });

  test("the question button opens a new conversation and then the message box", async () => {
    const { app, http } = await completed();
    http.on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })));

    await app.turns.onStarter("question");

    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
    expect(app.state.status).toBe("collecting");
    expect(window.document.activeElement).toBe(app.dom.message);
    expect(http.to("POST", NEW_TURNS)).toHaveLength(0);
  });

  test("the message box stays open and typing there starts a new conversation", async () => {
    const { app, http } = await completed();
    http
      .on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })))
      .on("POST", NEW_TURNS, ok(turnResponse({ session_id: SECOND_SESSION_ID, revision: 1 })));
    app.dom.message.value = "I also have a billing question";
    app.view.updateControls();

    expect(app.dom.message.readOnly).toBe(false);
    expect(app.dom.send.disabled).toBe(false);
    await app.turns.onSend();

    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
    expect(JSON.parse(http.to("POST", NEW_TURNS)[0].options.body)).toMatchObject({
      text: "I also have a billing question",
    });
    expect(http.to("POST", TURNS)).toHaveLength(0);
  });

  test("if the new conversation cannot be created, the typed text is kept", async () => {
    const { app, http } = await completed();
    http.on("POST", SESSIONS, failure("provider_unavailable", 503));
    app.dom.message.value = "another question";
    app.view.updateControls();

    await app.turns.onSend();

    expect(app.dom.message.value).toBe("another question");
    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(http.to("POST", TURNS)).toHaveLength(0);
  });

  test("if the new conversation cannot be created, nothing is sent", async () => {
    const { app, http } = await completed();
    http.on("POST", SESSIONS, failure("provider_unavailable", 503));

    await app.turns.onStarter("report");

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.state.status).toBe("completed");
    expect(http.to("POST", TURNS)).toHaveLength(0);
    expect(http.to("POST", NEW_TURNS)).toHaveLength(0);
  });

  test("a booking confirmation that asks if there is more brings the buttons back", async () => {
    const { app } = await running();
    await app.presenter.applyTurnResponse(
      turnResponse({ reply: `Booked for Tuesday. ${EN.anything_else}`, revision: 4 }),
      "book",
      "text"
    );

    expect(app.dom.starters.hidden).toBe(false);
    expect(labels(app)).toHaveLength(4);
  });

  test("a hand-off offers no buttons", async () => {
    const { app } = await running();
    await app.presenter.applyTurnResponse(turnResponse({ status: "handed_off", reply: "A person will help." }), "angry", "text");

    expect(app.dom.starters.hidden).toBe(true);
  });
});
