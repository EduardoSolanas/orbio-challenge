import { beforeEach, describe, expect, jest, test } from "@jest/globals";

import { ES, SESSION_ID, buildApp, sessionView, turnResponse } from "./support/page.js";

let app;
let presenter;
let state;
let dom;

beforeEach(() => {
  app = buildApp();
  ({ presenter, state, dom } = app);
});

describe("retry button", () => {
  test.each(["resume", "create", "confirmation"])("a failed %s offers the retry button", (kind) => {
    presenter.setRetry(kind, { x: 1 });

    expect(state.retryKind).toBe(kind);
    expect(state.retryPayload).toEqual({ x: 1 });
    expect(dom.retry.hidden).toBe(false);
  });

  test("a failed turn is resent from the composer, so the button stays hidden", () => {
    presenter.setRetry("turn", { text: "hi" });

    expect(state.retryKind).toBe("turn");
    expect(dom.retry.hidden).toBe(true);
  });

  test("clearRetry forgets the failure", () => {
    presenter.setRetry("create", null);
    presenter.clearRetry();

    expect(state.retryKind).toBeNull();
    expect(state.retryPayload).toBeNull();
    expect(dom.retry.hidden).toBe(true);
  });
});

describe("applySessionView", () => {
  test("loads the whole session into the state and the page", async () => {
    await presenter.applySessionView(
      sessionView({
        revision: 4,
        status: "awaiting_confirmation",
        summary: "Summary",
        pending_review_revision: 4,
      })
    );

    expect(state.sessionId).toBe(SESSION_ID);
    expect(app.store.load()).toBe(SESSION_ID);
    expect(state.revision).toBe(4);
    expect(state.status).toBe("awaiting_confirmation");
    expect(state.messages).toHaveLength(1);
    expect(dom.log.children).toHaveLength(1);
    expect(dom.review.textContent).toBe("Summary");
    expect(dom.confirm.hidden).toBe(false);
  });

  test("does not publish the id when asked not to", async () => {
    await presenter.applySessionView(sessionView(), { publishId: false });

    expect(state.sessionId).toBeNull();
    expect(app.store.load()).toBeNull();
  });

  test("resets the verification form, the time buttons and the orders flag", async () => {
    state.orderVerificationRequired = true;
    state.slotOptions = [{ starts_at: "a", ends_at: "b" }];
    dom.postcode.value = "SW1A";

    await presenter.applySessionView(sessionView());

    expect(state.orderVerificationRequired).toBe(false);
    expect(state.slotOptions).toEqual([]);
    expect(dom.postcode.value).toBe("");
    expect(dom.verification.hidden).toBe(true);
  });

  test("a Spanish session switches the page language", async () => {
    await presenter.applySessionView(sessionView({ reply_language: "es" }));

    expect(state.language).toBe("es");
    expect(document.documentElement.lang).toBe("es");
    expect(dom.send.getAttribute("aria-label")).toBe(ES["actions.send"]);
  });

  test("a missing language pack is loaded before anything changes", async () => {
    const fresh = buildApp();
    fresh.i18n.tables.delete("es");
    fresh.i18n.fetchFn = jest.fn(async () => ({ ok: true, status: 200, json: async () => ES }));

    await fresh.presenter.applySessionView(sessionView({ reply_language: "es" }));

    expect(fresh.i18n.fetchFn).toHaveBeenCalledWith("/api/locales/es", expect.anything());
    expect(fresh.state.language).toBe("es");
  });

  test("a result for an abandoned conversation is ignored", async () => {
    await presenter.applySessionView(sessionView(), { generation: state.generation + 1 });

    expect(state.sessionId).toBeNull();
    expect(dom.log.children).toHaveLength(0);
  });

  test("keeps only the fields the page uses from each message", async () => {
    const view = sessionView();
    view.messages[0].sentiment = "neutral";

    await presenter.applySessionView(view);

    expect(Object.keys(state.messages[0]).sort()).toEqual(
      ["citations", "id", "input_mode", "language", "role", "speech_available", "text"].sort()
    );
  });

  test("tolerates a session without messages or clarifications", async () => {
    const view = sessionView();
    delete view.messages;
    delete view.pending_clarifications;

    await presenter.applySessionView(view);

    expect(state.messages).toEqual([]);
    expect(state.pendingClarifications).toEqual({});
  });
});

describe("applyTurnResponse", () => {
  test("adds the customer's message and the reply", async () => {
    state.sessionId = SESSION_ID;

    await presenter.applyTurnResponse(turnResponse({ revision: 3 }), "My meter is broken", "text");

    expect(state.revision).toBe(3);
    expect(state.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(state.messages[0]).toMatchObject({ text: "My meter is broken", input_mode: "text" });
    expect(state.messages[1]).toMatchObject({ text: "Which area do you need help with?", input_mode: "system" });
    expect(dom.log.children).toHaveLength(2);
  });

  test("a null customer text adds only the reply", async () => {
    await presenter.applyTurnResponse(turnResponse(), null, null);

    expect(state.messages.map((m) => m.role)).toEqual(["assistant"]);
  });

  test("offered time windows become buttons; a later reply removes them", async () => {
    const slots = [{ starts_at: "2026-11-02T09:00", ends_at: "2026-11-02T11:00" }];

    await presenter.applyTurnResponse(turnResponse({ slot_options: slots }), "book", "text");
    expect(dom.slotPicker.querySelectorAll("button")).toHaveLength(1);

    await presenter.applyTurnResponse(turnResponse({ slot_options: undefined }), "thanks", "text");
    expect(dom.slotPicker.querySelectorAll("button")).toHaveLength(0);
  });

  test("asking for verification shows the form once and focuses the first field", async () => {
    await presenter.applyTurnResponse(turnResponse({ order_verification_required: true }), "status", "text");

    expect(state.orderVerificationRequired).toBe(true);
    expect(dom.verification.hidden).toBe(false);
    expect(document.activeElement).toBe(dom.postcode);

    dom.postcode.value = "SW1A";
    dom.message.focus();
    await presenter.applyTurnResponse(turnResponse({ order_verification_required: true }), "again", "text");

    expect(dom.postcode.value).toBe("SW1A");
    expect(document.activeElement).toBe(dom.message);
  });

  test("a reply that no longer needs verification clears the form error", async () => {
    state.orderVerificationRequired = true;
    app.view.setVerificationError("wrong");

    await presenter.applyTurnResponse(turnResponse(), "hello", "text");

    expect(state.orderVerificationRequired).toBe(false);
    expect(dom.verificationError.hidden).toBe(true);
  });

  test("a Spanish reply switches the language", async () => {
    await presenter.applyTurnResponse(turnResponse({ reply_language: "es" }), "Hola", "text");

    expect(state.language).toBe("es");
    expect(document.documentElement.lang).toBe("es");
  });

  test("a reply for an abandoned conversation is ignored", async () => {
    await presenter.applyTurnResponse(turnResponse(), "hi", "text", state.generation + 1);

    expect(state.messages).toEqual([]);
    expect(state.revision).toBe(0);
  });

  test("the review appears when the reply says the details await confirmation", async () => {
    await presenter.applyTurnResponse(
      turnResponse({ status: "awaiting_confirmation", pending_review_revision: 5, summary: "Review" }),
      "high urgency",
      "text"
    );

    expect(dom.review.hidden).toBe(false);
    expect(dom.review.textContent).toBe("Review");
    expect(dom.confirm.hidden).toBe(false);
  });
});
