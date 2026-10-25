import { describe, expect, test } from "@jest/globals";

import {
  EN,
  ScriptedHttp,
  SESSION_ID,
  deferred,
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
const CONFIRM = `${SESSION}/confirm`;
const BOOKINGS = `${SESSION}/bookings`;
const VERIFY = `${SESSION}/order-verification`;
const FIRST_REQUEST_ID = "00000000-0000-4000-8000-000000000001";

async function running() {
  const http = new ScriptedHttp().on("POST", SESSIONS, ok(sessionView()));
  const app = await startApp({ http });
  return { app, http };
}

function type(app, text) {
  app.dom.message.value = text;
  app.dom.message.dispatchEvent(new Event("input"));
}

const bodyOf = (http, method, url, index = 0) => JSON.parse(http.to(method, url)[index].options.body);

async function awaitingConfirmation(app, revision = 3) {
  await app.presenter.applyTurnResponse(
    turnResponse({ status: "awaiting_confirmation", pending_review_revision: revision, summary: "Review", revision }),
    "all details",
    "text"
  );
}

describe("sending a message", () => {
  test("posts the draft with the current revision and shows the reply", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse({ revision: 1 })));
    type(app, "My meter is broken");

    await app.turns.onSend();

    expect(bodyOf(http, "POST", TURNS)).toEqual({
      request_id: FIRST_REQUEST_ID,
      expected_revision: 0,
      text: "My meter is broken",
      input_mode: "text",
    });
    expect(app.state.revision).toBe(1);
    expect(app.state.messages.map((m) => m.role)).toEqual(["assistant", "user", "assistant"]);
    expect(app.dom.message.value).toBe("");
    expect(app.state.pending).toBe(false);
    expect(app.state.pendingTurnMessage).toBeNull();
    expect(app.state.retryKind).toBeNull();
  });

  test("a dictated draft is sent as a transcript", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse()));
    app.state.inputMode = "transcript";
    type(app, "order NS-000123 is broken");
    app.state.inputMode = "transcript";

    await app.turns.onSend();

    expect(bodyOf(http, "POST", TURNS).input_mode).toBe("transcript");
    expect(app.state.inputMode).toBe("text");
  });

  test("shows the message and the thinking dots while waiting", async () => {
    const { app, http } = await running();
    const reply = deferred();
    http.on("POST", TURNS, () => reply.promise);
    type(app, "Hello");

    const sending = app.turns.onSend();
    await flush();

    expect(app.dom.log.querySelector("[role='status']")).not.toBeNull();
    expect(app.dom.log.textContent).toContain("Hello");
    expect(app.dom.send.disabled).toBe(true);

    reply.resolve(ok(turnResponse()));
    await sending;

    expect(app.dom.log.querySelector("[role='status']")).toBeNull();
  });

  test.each([
    ["an empty draft", ""],
    ["a blank draft", "   "],
    ["a draft over the limit", "x".repeat(4001)],
  ])("does nothing for %s", async (_label, text) => {
    const { app, http } = await running();
    type(app, text);

    await app.turns.onSend();

    expect(http.to("POST", TURNS)).toHaveLength(0);
  });

  test("does nothing while another request is in flight", async () => {
    const { app, http } = await running();
    type(app, "Hello");
    app.state.pending = true;

    await app.turns.onSend();

    expect(http.to("POST", TURNS)).toHaveLength(0);
  });

  test("does nothing without a session", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, new Error("offline"));
    const app = await startApp({ http });

    await app.turns.submitTurnPayload({ text: "hi", input_mode: "text" });

    expect(http.to("POST", /turns/)).toHaveLength(0);
  });

  test("a reply that needs verification shows the form", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, ok(turnResponse({ order_verification_required: true })));
    type(app, "Where is my order?");

    await app.turns.onSend();

    expect(app.dom.verification.hidden).toBe(false);
    expect(app.dom.message.readOnly).toBe(false);
  });

  test("a reply for a conversation that was abandoned meanwhile is dropped", async () => {
    const { app, http } = await running();
    const reply = deferred();
    http.on("POST", TURNS, () => reply.promise);
    type(app, "Hello");

    const sending = app.turns.onSend();
    await flush();
    app.state.generation += 1;
    reply.resolve(ok(turnResponse()));
    await sending;

    expect(app.state.messages.map((m) => m.role)).toEqual(["assistant"]);
    expect(app.state.revision).toBe(0);
  });
});

describe("a turn that fails", () => {
  test("an invalid request shows its message, restores the draft and offers no retry", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, failure("invalid_request", 422));
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.dom.alert.textContent).toBe(EN["errors.invalid_request"]);
    expect(app.dom.message.value).toBe("Hello");
    expect(app.state.retryKind).toBeNull();
    expect(app.state.pending).toBe(false);
  });

  test.each([
    ["a retryable error", failure("provider_unavailable", 503, { retryable: true })],
    ["a server error", failure("internal_error", 500)],
  ])("%s keeps the same payload for resending", async (_label, response) => {
    const { app, http } = await running();
    http.on("POST", TURNS, response);
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.state.retryKind).toBe("turn");
    expect(app.state.retryPayload.text).toBe("Hello");
    expect(app.state.retryPayload.request_id).toBe(FIRST_REQUEST_ID);
    expect(app.dom.message.value).toBe("Hello");
  });

  test("a network failure alerts, restores the draft and keeps the payload", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, new Error("offline"));
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
    expect(app.dom.message.value).toBe("Hello");
    expect(app.state.retryKind).toBe("turn");
  });

  test("resending uses the original request id", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, new Error("offline"), ok(turnResponse()));
    type(app, "Hello");
    await app.turns.onSend();

    await app.session.onRetry();

    const ids = http.to("POST", TURNS).map((call) => JSON.parse(call.options.body).request_id);
    expect(ids).toEqual([FIRST_REQUEST_ID, FIRST_REQUEST_ID]);
    expect(app.state.messages.at(-1).role).toBe("assistant");
  });

  test("a revision conflict refreshes the session, keeps the draft and says so", async () => {
    const { app, http } = await running();
    http
      .on("POST", TURNS, failure("revision_conflict", 409, { current_revision: 5 }))
      .on("GET", SESSION, ok(sessionView({ revision: 5 })));
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.state.revision).toBe(5);
    expect(app.dom.alert.textContent).toBe(EN["errors.revision_conflict"]);
    expect(app.dom.message.value).toBe("Hello");
    expect(app.state.retryKind).toBeNull();
    expect(app.state.retryPayload).toBeNull();
  });

  test.each([
    ["the refresh is refused", failure("storage_unavailable", 503)],
    ["the refresh cannot be reached", new Error("offline")],
  ])("a revision conflict still reports it when %s", async (_label, refresh) => {
    const { app, http } = await running();
    http.on("POST", TURNS, failure("revision_conflict", 409)).on("GET", SESSION, refresh);
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.dom.alert.textContent).toBe(EN["errors.revision_conflict"]);
    expect(app.state.revision).toBe(0);
    expect(app.dom.message.value).toBe("Hello");
  });

  test("another conflict shows its message and restores the draft", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, failure("turn_limit", 409));
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.dom.alert.textContent).toBe(EN["errors.turn_limit"]);
    expect(app.dom.message.value).toBe("Hello");
    expect(app.state.retryKind).toBeNull();
  });

  test("a retryable conflict offers resending", async () => {
    const { app, http } = await running();
    http.on("POST", TURNS, failure("session_busy", 409, { retryable: true }));
    type(app, "Hello");

    await app.turns.onSend();

    expect(app.state.retryKind).toBe("turn");
  });
});

describe("confirming the review", () => {
  test("posts the exact review revision, applies the reply and reloads the session", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app, 3);
    http
      .on("POST", CONFIRM, ok(turnResponse({ status: "completed", revision: 4, reply: "Saved locally." })))
      .on("GET", SESSION, ok(sessionView({ status: "completed", revision: 4 })));

    await app.turns.onConfirm();

    expect(bodyOf(http, "POST", CONFIRM)).toEqual({
      request_id: FIRST_REQUEST_ID,
      expected_revision: 3,
      review_revision: 3,
    });
    expect(app.state.status).toBe("completed");
    expect(app.dom.message.readOnly).toBe(true);
    expect(app.dom.review.hidden).toBe(true);
    expect(http.to("GET", SESSION)).toHaveLength(1);
    expect(app.state.pending).toBe(false);
  });

  test("does nothing when there is nothing to confirm", async () => {
    const { app, http } = await running();

    await app.turns.onConfirm();

    expect(http.to("POST", CONFIRM)).toHaveLength(0);
  });

  test("does nothing while another request is in flight", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    app.state.pending = true;

    await app.turns.onConfirm();

    expect(http.to("POST", CONFIRM)).toHaveLength(0);
  });

  test("a retryable failure is retried with identical bytes", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    http
      .on("POST", CONFIRM, failure("provider_unavailable", 503, { retryable: true }), ok(turnResponse({ status: "completed" })))
      .on("GET", SESSION, ok(sessionView({ status: "completed" })));
    await app.turns.onConfirm();
    expect(app.state.retryKind).toBe("confirmation");
    expect(app.dom.retry.hidden).toBe(false);

    await app.session.onRetry();

    const [first, second] = http.to("POST", CONFIRM);
    expect(second.options.body).toBe(first.options.body);
    expect(app.state.retryKind).toBeNull();
    expect(app.state.status).toBe("completed");
  });

  test("a refusal that cannot succeed on retry clears the retry offer", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    http.on("POST", CONFIRM, failure("stale_review", 409));

    await app.turns.onConfirm();

    expect(app.dom.alert.textContent).toBe(EN["errors.stale_review"]);
    expect(app.state.retryKind).toBeNull();
  });

  test("a server error is retryable even without the flag", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    http.on("POST", CONFIRM, failure("internal_error", 500));

    await app.turns.onConfirm();

    expect(app.state.retryKind).toBe("confirmation");
  });

  test("a network failure offers retry", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    http.on("POST", CONFIRM, new Error("offline"));

    await app.turns.onConfirm();

    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
    expect(app.state.retryKind).toBe("confirmation");
  });

  test("a failure to reload afterwards does not undo the confirmation", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    http
      .on("POST", CONFIRM, ok(turnResponse({ status: "completed", revision: 4 })))
      .on("GET", SESSION, new Error("offline"));

    await app.turns.onConfirm();

    expect(app.state.status).toBe("completed");
    expect(app.dom.alert.hidden).toBe(true);
  });

  test("a result for an abandoned conversation is dropped", async () => {
    const { app, http } = await running();
    await awaitingConfirmation(app);
    const reply = deferred();
    http.on("POST", CONFIRM, () => reply.promise);

    const confirming = app.turns.onConfirm();
    await flush();
    app.state.generation += 1;
    reply.resolve(ok(turnResponse({ status: "completed" })));
    await confirming;

    expect(app.state.status).toBe("awaiting_confirmation");
  });
});

describe("booking a time", () => {
  const slot = { starts_at: "2026-11-02T09:00", ends_at: "2026-11-02T11:00" };

  async function withOptions() {
    const { app, http } = await running();
    await app.presenter.applyTurnResponse(turnResponse({ slot_options: [slot], revision: 2 }), "book", "text");
    return { app, http };
  }

  test("posts the chosen window, shows the confirmation and reloads the session", async () => {
    const { app, http } = await withOptions();
    http
      .on("POST", BOOKINGS, ok(turnResponse({ revision: 3, reply: "Booked: ..." })))
      .on("GET", SESSION, ok(sessionView({ revision: 3 })));

    await app.turns.onBookSlot(slot);

    expect(bodyOf(http, "POST", BOOKINGS)).toEqual({
      request_id: FIRST_REQUEST_ID,
      expected_revision: 2,
      starts_at: "2026-11-02T09:00",
    });
    expect(app.state.revision).toBe(3);
    expect(app.state.slotOptions).toEqual([]);
    expect(app.dom.slotPicker.hidden).toBe(true);
    expect(app.state.pending).toBe(false);
  });

  test("a window that was just taken removes the buttons and says why", async () => {
    const { app, http } = await withOptions();
    http.on("POST", BOOKINGS, failure("slot_unavailable", 409));

    await app.turns.onBookSlot(slot);

    expect(app.dom.alert.textContent).toBe(EN["errors.slot_unavailable"]);
    expect(app.state.slotOptions).toEqual([]);
    expect(app.dom.slotPicker.hidden).toBe(true);
  });

  test("any other refusal keeps the buttons", async () => {
    const { app, http } = await withOptions();
    http.on("POST", BOOKINGS, failure("verification_required", 403));

    await app.turns.onBookSlot(slot);

    expect(app.dom.alert.textContent).toBe(EN["errors.verification_required"]);
    expect(app.state.slotOptions).toHaveLength(1);
  });

  test("a network failure alerts and keeps the buttons", async () => {
    const { app, http } = await withOptions();
    http.on("POST", BOOKINGS, new Error("offline"));

    await app.turns.onBookSlot(slot);

    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
    expect(app.state.slotOptions).toHaveLength(1);
    expect(app.state.pending).toBe(false);
  });

  test("does nothing while busy or without a session", async () => {
    const { app, http } = await withOptions();
    app.state.pending = true;

    await app.turns.onBookSlot(slot);

    expect(http.to("POST", BOOKINGS)).toHaveLength(0);
  });

  test("clicking a rendered button books that window", async () => {
    const { app, http } = await withOptions();
    http.on("POST", BOOKINGS, ok(turnResponse({ revision: 3 }))).on("GET", SESSION, ok(sessionView({ revision: 3 })));

    app.dom.slotPicker.querySelector("button").click();
    await flush();
    await flush();

    expect(bodyOf(http, "POST", BOOKINGS).starts_at).toBe("2026-11-02T09:00");
  });

  test("a result for an abandoned conversation is dropped", async () => {
    const { app, http } = await withOptions();
    const reply = deferred();
    http.on("POST", BOOKINGS, () => reply.promise);

    const booking = app.turns.onBookSlot(slot);
    await flush();
    app.state.generation += 1;
    reply.resolve(ok(turnResponse({ revision: 3 })));
    await booking;

    expect(app.state.revision).toBe(2);
  });
});

describe("verifying the account", () => {
  async function asking() {
    const { app, http } = await running();
    await app.presenter.applyTurnResponse(turnResponse({ order_verification_required: true }), "status", "text");
    app.dom.postcode.value = "SW1A 1AA";
    app.dom.code.value = "246810";
    return { app, http };
  }

  test("a correct code hides the form, clears the fields and says verified", async () => {
    const { app, http } = await asking();
    http.on("POST", VERIFY, ok({ verified: true, error: null }));

    await app.turns.onVerifyOrder();

    expect(bodyOf(http, "POST", VERIFY)).toEqual({ delivery_postcode: "SW1A 1AA", verification_code: "246810" });
    expect(app.state.orderVerificationRequired).toBe(false);
    expect(app.dom.verification.hidden).toBe(true);
    expect(app.dom.postcode.value).toBe("");
    expect(app.dom.verificationStatus.textContent).toBe(EN["auth.verified"]);
    expect(app.state.verificationPending).toBe(false);
  });

  test("a wrong code shows one generic message and keeps the form", async () => {
    const { app, http } = await asking();
    http.on("POST", VERIFY, ok({ verified: false, error: "verification_failed" }));

    await app.turns.onVerifyOrder();

    expect(app.dom.verificationError.textContent).toBe(EN["auth.verification_failed"]);
    expect(app.dom.verification.hidden).toBe(false);
    expect(app.dom.verificationStatus.hidden).toBe(true);
  });

  test("a refusal such as too many attempts shows its own message", async () => {
    const { app, http } = await asking();
    http.on("POST", VERIFY, failure("too_many_verification_attempts", 429));

    await app.turns.onVerifyOrder();

    expect(app.dom.verificationError.textContent).toBe(EN["errors.too_many_verification_attempts"]);
  });

  test("a network failure shows the transport message", async () => {
    const { app, http } = await asking();
    http.on("POST", VERIFY, Object.assign(new Error("slow"), { name: "AbortError" }));

    await app.turns.onVerifyOrder();

    expect(app.dom.verificationError.textContent).toBe(EN["errors.provider_timeout"]);
    expect(app.state.verificationPending).toBe(false);
  });

  test.each([
    ["verification is not being asked for", (app) => (app.state.orderVerificationRequired = false)],
    ["the postcode is empty", (app) => (app.dom.postcode.value = " ")],
    ["the code is empty", (app) => (app.dom.code.value = "")],
    ["a check is already running", (app) => (app.state.verificationPending = true)],
  ])("does nothing when %s", async (_label, change) => {
    const { app, http } = await asking();
    change(app);

    await app.turns.onVerifyOrder();

    expect(http.to("POST", VERIFY)).toHaveLength(0);
  });

  test("a result for an abandoned conversation is dropped", async () => {
    const { app, http } = await asking();
    const reply = deferred();
    http.on("POST", VERIFY, () => reply.promise);

    const verifying = app.turns.onVerifyOrder();
    await flush();
    app.state.generation += 1;
    reply.resolve(ok({ verified: true, error: null }));
    await verifying;

    expect(app.state.orderVerificationRequired).toBe(true);
  });
});
