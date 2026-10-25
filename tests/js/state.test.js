import { describe, expect, test } from "@jest/globals";

import { SessionStore } from "../../web/js/session_store.js";
import { ChatState } from "../../web/js/state.js";

describe("ChatState", () => {
  test("starts as an empty, idle English conversation", () => {
    const state = new ChatState();

    expect(state.sessionId).toBeNull();
    expect(state.status).toBe("collecting");
    expect(state.language).toBe("en");
    expect(state.messages).toEqual([]);
    expect(state.slotOptions).toEqual([]);
    expect(state.isBusy()).toBe(false);
    expect(state.isClosed()).toBe(false);
  });

  test("two instances never share mutable fields", () => {
    const first = new ChatState();
    first.messages.push({ text: "x" });
    first.pendingClarifications.order_number = {};

    expect(new ChatState().messages).toEqual([]);
    expect(new ChatState().pendingClarifications).toEqual({});
  });

  test.each(["completed", "handed_off"])("a %s conversation is closed", (status) => {
    const state = new ChatState();
    state.status = status;

    expect(state.isClosed()).toBe(true);
  });

  test.each(["collecting", "awaiting_confirmation"])("a %s conversation is open", (status) => {
    const state = new ChatState();
    state.status = status;

    expect(state.isClosed()).toBe(false);
  });

  test("confirmation needs a review token, no open issues and the awaiting status", () => {
    const state = new ChatState();
    expect(state.canConfirm()).toBe(false);

    state.status = "awaiting_confirmation";
    expect(state.canConfirm()).toBe(false);

    state.pendingReviewRevision = 4;
    expect(state.canConfirm()).toBe(true);

    state.pendingClarifications = { urgency: { code: "invalid" } };
    expect(state.issuesPresent()).toBe(true);
    expect(state.canConfirm()).toBe(false);

    state.pendingClarifications = null;
    expect(state.issuesPresent()).toBe(false);
  });

  test.each([
    "pending",
    "createPending",
    "sttPending",
    "recordingPending",
    "resumePending",
    "resumeFailed",
    "verificationPending",
  ])("%s makes the conversation busy", (flag) => {
    const state = new ChatState();
    state[flag] = true;

    expect(state.isBusy()).toBe(true);
  });

  test("a live recording makes the conversation busy", () => {
    const state = new ChatState();
    state.recording = {};

    expect(state.isBusy()).toBe(true);
  });

  test("bumpPlayback returns increasing tokens", () => {
    const state = new ChatState();

    expect(state.bumpPlayback()).toBe(1);
    expect(state.bumpPlayback()).toBe(2);
    expect(state.playbackToken).toBe(2);
  });
});

describe("SessionStore", () => {
  const ID = "11111111-1111-4111-8111-111111111111";

  function memoryStorage(initial = {}) {
    const data = new Map(Object.entries(initial));
    return {
      getItem: (key) => (data.has(key) ? data.get(key) : null),
      setItem: (key, value) => data.set(key, value),
      removeItem: (key) => data.delete(key),
      has: (key) => data.has(key),
    };
  }

  test("saves and loads a canonical id", () => {
    const storage = memoryStorage();
    const store = new SessionStore(storage);

    store.save(ID);

    expect(storage.getItem("northstar.session_id")).toBe(ID);
    expect(store.load()).toBe(ID);
  });

  test("saving nothing clears the stored id", () => {
    const storage = memoryStorage({ "northstar.session_id": ID });
    const store = new SessionStore(storage);

    store.save(null);

    expect(storage.has("northstar.session_id")).toBe(false);
    expect(store.load()).toBeNull();
  });

  test("a malformed stored value is discarded", () => {
    const storage = memoryStorage({ "northstar.session_id": "not-a-uuid" });

    expect(new SessionStore(storage).load()).toBeNull();
    expect(storage.has("northstar.session_id")).toBe(false);
  });

  test("only a lowercase uuid is canonical", () => {
    const lower = "abcdef01-2345-4678-89ab-cdef01234567";

    expect(SessionStore.isCanonicalUuid(lower)).toBe(true);
    expect(SessionStore.isCanonicalUuid(lower.toUpperCase())).toBe(false);
    expect(SessionStore.isCanonicalUuid(`${lower}0`)).toBe(false);
    expect(SessionStore.isCanonicalUuid(42)).toBe(false);
  });

  test("a different storage key can be used", () => {
    const storage = memoryStorage();
    new SessionStore(storage, "other").save(ID);

    expect(storage.getItem("other")).toBe(ID);
    expect(new SessionStore(storage).load()).toBeNull();
  });
});
