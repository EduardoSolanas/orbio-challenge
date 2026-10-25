import { describe, expect, jest, test } from "@jest/globals";

import {
  EN,
  ES,
  ScriptedHttp,
  SECOND_SESSION_ID,
  SESSION_ID,
  buildApp,
  deferred,
  failure,
  flush,
  ok,
  sessionView,
  startApp,
} from "./support/page.js";

const SESSIONS = "/api/sessions";
const SESSION = `/api/sessions/${SESSION_ID}`;

describe("bootstrap without a stored session", () => {
  test("creates a session, shows its greeting and enables the composer", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, ok(sessionView()));

    const app = await startApp({ http });

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.store.load()).toBe(SESSION_ID);
    expect(app.dom.log.textContent).toContain("Hi, welcome to Northstar Energy");
    expect(app.state.createPending).toBe(false);
    expect(app.dom.alert.hidden).toBe(true);
    expect(http.to("POST", SESSIONS)).toHaveLength(1);
  });

  test("a refused creation shows the error and offers retry", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, failure("storage_unavailable", 503, { retryable: true }));

    const app = await startApp({ http });

    expect(app.dom.alert.textContent).toBe(EN["errors.storage_unavailable"]);
    expect(app.state.retryKind).toBe("create");
    expect(app.dom.retry.hidden).toBe(false);
    expect(app.state.createPending).toBe(false);
    expect(app.state.sessionId).toBeNull();
  });

  test("a network failure shows the transport message and offers retry", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, new Error("offline"));

    const app = await startApp({ http });

    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
    expect(app.state.retryKind).toBe("create");
  });

  test("a request that outlives a newer conversation is ignored", async () => {
    const slow = deferred();
    const http = new ScriptedHttp().on("POST", SESSIONS, () => slow.promise);
    const app = buildApp({ http });

    const pending = app.session.bootstrap();
    app.state.generation += 1;
    slow.resolve(ok(sessionView()));
    await pending;

    expect(app.state.sessionId).toBeNull();
  });
});

describe("bootstrap with a stored session", () => {
  test("resumes it and shows the saved conversation", async () => {
    const view = sessionView({
      revision: 2,
      messages: [
        ...sessionView().messages,
        { id: "c", role: "user", text: "My meter is broken", input_mode: "text", language: "en", citations: [] },
      ],
    });
    const http = new ScriptedHttp().on("GET", SESSION, ok(view));

    const app = await startApp({ http, storedId: SESSION_ID });

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.state.revision).toBe(2);
    expect(app.dom.log.textContent).toContain("My meter is broken");
    expect(app.state.resumePending).toBe(false);
    expect(http.to("POST", SESSIONS)).toHaveLength(0);
  });

  test("an unknown stored session is forgotten and a new one is created", async () => {
    const http = new ScriptedHttp()
      .on("GET", SESSION, failure("session_not_found", 404))
      .on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })));

    const app = await startApp({ http, storedId: SESSION_ID });

    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
    expect(app.store.load()).toBe(SECOND_SESSION_ID);
  });

  test("a failed resume keeps the id, locks the composer and offers retry", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, failure("storage_corrupt", 503));

    const app = await startApp({ http, storedId: SESSION_ID });

    expect(app.state.resumeFailed).toBe(true);
    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.state.retryKind).toBe("resume");
    expect(app.state.retryPayload).toEqual({ sessionId: SESSION_ID });
    expect(app.dom.alert.textContent).toBe(EN["errors.storage_corrupt"]);
    expect(app.dom.message.readOnly).toBe(true);
    expect(app.dom.retry.hidden).toBe(false);
    expect(app.store.load()).toBe(SESSION_ID);
  });

  test("a network failure while resuming also offers retry", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, Object.assign(new Error("slow"), { name: "AbortError" }));

    const app = await startApp({ http, storedId: SESSION_ID });

    expect(app.state.resumeFailed).toBe(true);
    expect(app.dom.alert.textContent).toBe(EN["errors.provider_timeout"]);
    expect(app.state.sessionId).toBe(SESSION_ID);
  });

  test("a missing language pack is reported without pretending the session loaded", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, ok(sessionView({ reply_language: "es" })));
    const app = buildApp({ http });
    app.i18n.tables.delete("es");
    app.i18n.fetchFn = async () => {
      throw new Error("offline");
    };
    app.store.save(SESSION_ID);

    await app.session.bootstrap();

    expect(app.dom.alert.textContent).toBe(EN["errors.locale_unavailable"]);
    expect(app.state.resumeFailed).toBe(true);
    expect(app.state.sessionId).toBeNull();
    expect(app.state.retryKind).toBe("resume");
  });

  test("a stored value that is not a session id is discarded and a new session is created", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, ok(sessionView()));
    const app = buildApp({ http });
    window.localStorage.setItem("northstar.session_id", "garbage");

    await app.session.bootstrap();

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(http.to("GET", /.*/)).toHaveLength(0);
  });

  test("a stored session that vanishes and cannot be recreated offers retry", async () => {
    const http = new ScriptedHttp()
      .on("GET", SESSION, failure("session_not_found", 404))
      .on("POST", SESSIONS, failure("storage_unavailable", 503));

    const app = await startApp({ http, storedId: SESSION_ID });

    expect(app.state.retryKind).toBe("create");
    expect(app.store.load()).toBeNull();
    expect(app.state.createPending).toBe(false);
  });

  test("a network failure while recreating a vanished session offers retry", async () => {
    const http = new ScriptedHttp()
      .on("GET", SESSION, failure("session_not_found", 404))
      .on("POST", SESSIONS, new Error("offline"));

    const app = await startApp({ http, storedId: SESSION_ID });

    expect(app.state.retryKind).toBe("create");
    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
  });
});

describe("retry", () => {
  test("does nothing when nothing failed", async () => {
    const http = new ScriptedHttp();
    const app = buildApp({ http });

    await app.session.onRetry();

    expect(http.calls).toHaveLength(0);
  });

  test("a failed resume is retried with the remembered id", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, failure("storage_corrupt", 503), ok(sessionView({ revision: 3 })));
    const app = await startApp({ http, storedId: SESSION_ID });

    await app.session.onRetry();

    expect(app.state.resumeFailed).toBe(false);
    expect(app.state.revision).toBe(3);
    expect(app.state.retryKind).toBeNull();
    expect(app.dom.alert.hidden).toBe(true);
  });

  test("a resume that fails again keeps the retry offer", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, failure("storage_corrupt", 503));
    const app = await startApp({ http, storedId: SESSION_ID });

    await app.session.onRetry();

    expect(app.state.resumeFailed).toBe(true);
    expect(app.state.retryKind).toBe("resume");
  });

  test("a resume retry that hits a network failure keeps the retry offer", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, failure("storage_corrupt", 503), new Error("offline"));
    const app = await startApp({ http, storedId: SESSION_ID });

    await app.session.onRetry();

    expect(app.state.retryKind).toBe("resume");
    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
  });

  test("a retry with no remembered id falls back to the stored one, or does nothing", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, ok(sessionView()));
    const app = buildApp({ http });
    app.state.retryKind = "resume";

    await app.session.onRetry();
    expect(http.calls).toHaveLength(0);

    app.store.save(SESSION_ID);
    await app.session.onRetry();
    expect(http.to("GET", SESSION)).toHaveLength(1);
  });

  test("a failed creation is retried by starting a new conversation", async () => {
    const http = new ScriptedHttp().on("POST", SESSIONS, failure("storage_unavailable", 503), ok(sessionView()));
    const app = await startApp({ http });

    await app.session.onRetry();

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.state.retryKind).toBeNull();
  });

  test("a failed turn and a failed confirmation are handed back to the turn controller", async () => {
    const app = buildApp();
    app.turns.submitTurnPayload = jest.fn();
    app.turns.submitConfirmationCommand = jest.fn();

    app.state.retryKind = "turn";
    app.state.retryPayload = { text: "hi" };
    await app.session.onRetry();
    expect(app.turns.submitTurnPayload).toHaveBeenCalledWith({ text: "hi" });

    app.state.retryKind = "confirmation";
    app.state.retryPayload = { body: "{}" };
    await app.session.onRetry();
    expect(app.turns.submitConfirmationCommand).toHaveBeenCalledWith({ body: "{}" });
  });

  test("a turn or confirmation retry without a payload does nothing", async () => {
    const app = buildApp();
    app.turns.submitTurnPayload = jest.fn();

    app.state.retryKind = "turn";
    app.state.retryPayload = null;
    await app.session.onRetry();

    expect(app.turns.submitTurnPayload).not.toHaveBeenCalled();
  });
});

describe("startNewIntake", () => {
  async function runningApp(http) {
    http.on("POST", SESSIONS, ok(sessionView()));
    return startApp({ http });
  }

  test("replaces the conversation with a fresh session and clears everything pending", async () => {
    const http = new ScriptedHttp();
    const app = await runningApp(http);
    app.state.slotOptions = [{ starts_at: "a", ends_at: "b" }];
    app.state.orderVerificationRequired = true;
    app.state.audioBlob = new Blob(["x"]);
    app.state.inputMode = "transcript";
    app.dom.message.value = "draft";
    const generation = app.state.generation;
    http.on("POST", SESSIONS, ok(sessionView({ session_id: SECOND_SESSION_ID })));

    await app.session.startNewIntake();

    expect(app.state.generation).toBe(generation + 1);
    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
    expect(app.store.load()).toBe(SECOND_SESSION_ID);
    expect(app.state.slotOptions).toEqual([]);
    expect(app.state.orderVerificationRequired).toBe(false);
    expect(app.state.audioBlob).toBeNull();
    expect(app.state.inputMode).toBe("text");
    expect(app.dom.message.value).toBe("");
    expect(app.state.createPending).toBe(false);
  });

  test("aborts a transcription in progress and stops any playback", async () => {
    const app = await runningApp(new ScriptedHttp());
    const abort = jest.fn();
    app.state.sttAbort = { abort };
    app.state.sttPending = true;
    app.state.speechPending = { generation: 0, messageId: "m", token: 1 };
    app.state.objectUrl = "blob:reply";

    await app.session.startNewIntake();

    expect(abort).toHaveBeenCalled();
    expect(app.state.sttAbort).toBeNull();
    expect(app.state.sttPending).toBe(false);
    expect(app.state.speechPending).toBeNull();
    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith("blob:reply");
  });

  test("an abort that throws does not stop the new conversation", async () => {
    const app = await runningApp(new ScriptedHttp());
    app.state.sttAbort = {
      abort() {
        throw new Error("boom");
      },
    };

    await app.session.startNewIntake();

    expect(app.state.sttAbort).toBeNull();
    expect(app.state.sessionId).toBe(SESSION_ID);
  });

  test("discards a live recording", async () => {
    const app = await runningApp(new ScriptedHttp());
    const discard = jest.fn();
    app.state.recording = { discard };

    await app.session.startNewIntake();

    expect(discard).toHaveBeenCalled();
    expect(app.state.recording).toBeNull();
  });

  test("a refused creation keeps the old conversation visible and offers retry", async () => {
    const http = new ScriptedHttp();
    const app = await runningApp(http);
    http.on("POST", SESSIONS, failure("storage_unavailable", 503));

    await app.session.startNewIntake();

    expect(app.state.sessionId).toBe(SESSION_ID);
    expect(app.dom.log.textContent).toContain("Hi, welcome");
    expect(app.state.retryKind).toBe("create");
    expect(app.dom.alert.textContent).toBe(EN["errors.storage_unavailable"]);
    expect(app.state.createPending).toBe(false);
  });

  test("a network failure offers retry", async () => {
    const http = new ScriptedHttp();
    const app = await runningApp(http);
    http.on("POST", SESSIONS, new Error("offline"));

    await app.session.startNewIntake();

    expect(app.state.retryKind).toBe("create");
    expect(app.dom.alert.textContent).toBe(EN["errors.provider_unavailable"]);
  });

  test("starting again while the first is still waiting leaves only the newest result", async () => {
    const http = new ScriptedHttp();
    const app = await runningApp(http);
    const first = deferred();
    http.on("POST", SESSIONS, () => first.promise, ok(sessionView({ session_id: SECOND_SESSION_ID })));

    const older = app.session.startNewIntake();
    await flush();
    const newer = app.session.startNewIntake();
    first.resolve(ok(sessionView({ session_id: "33333333-3333-4333-8333-333333333333" })));
    await Promise.all([older, newer]);

    expect(app.state.sessionId).toBe(SECOND_SESSION_ID);
  });

  test("a language switch from the previous conversation does not leak into the new one", async () => {
    const http = new ScriptedHttp();
    const app = await runningApp(http);
    app.i18n.seed("es", ES);
    app.state.language = "es";
    http.on("POST", SESSIONS, ok(sessionView({ reply_language: "en", session_id: SECOND_SESSION_ID })));

    await app.session.startNewIntake();

    expect(app.state.language).toBe("en");
    expect(app.dom.send.getAttribute("aria-label")).toBe(EN["actions.send"]);
  });
});

describe("a Spanish session", () => {
  test("a resumed Spanish session shows Spanish labels", async () => {
    const http = new ScriptedHttp().on("GET", SESSION, ok(sessionView({ reply_language: "es" })));
    const app = buildApp({ http });
    app.store.save(SESSION_ID);

    await app.session.bootstrap();

    expect(app.state.language).toBe("es");
    expect(app.dom.send.getAttribute("aria-label")).toBe(ES["actions.send"]);
  });
});
