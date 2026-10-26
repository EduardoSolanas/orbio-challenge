import { describe, expect, test } from "@jest/globals";

import {
  EN,
  ES,
  ScriptedHttp,
  SESSION_ID,
  deferred,
  ok,
  sessionView,
  startApp,
  turnResponse,
} from "./support/page.js";

const SESSIONS = "/api/sessions";
const TURNS = `/api/sessions/${SESSION_ID}/turns`;

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
    ["the conversation is closed", (s) => (s.status = "completed")],
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

  test("nothing is sent without a session, while busy, or once the conversation is closed", async () => {
    const { app, http } = await running();

    app.state.pending = true;
    await app.turns.onStarter("report");
    app.state.pending = false;
    app.state.status = "completed";
    await app.turns.onStarter("report");
    app.state.status = "collecting";
    app.state.sessionId = null;
    await app.turns.onStarter("report");

    expect(http.to("POST", TURNS)).toHaveLength(0);
  });
});
