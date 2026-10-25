import { beforeEach, describe, expect, test } from "@jest/globals";

import { ApiClient } from "../../web/js/api.js";
import { DEADLINES } from "../../web/js/config.js";
import { I18n } from "../../web/js/i18n.js";
import { EN, ES, FakeResponse, ScriptedHttp, SESSION_ID, failure, ok } from "./support/page.js";

let http;
let language;
let api;

beforeEach(() => {
  http = new ScriptedHttp();
  language = "en";
  const i18n = new I18n({ document });
  i18n.seed("en", EN);
  i18n.seed("es", ES);
  api = new ApiClient({ http, i18n, getLanguage: () => language });
  for (const [method, pattern] of [
    ["POST", /.*/],
    ["GET", /.*/],
  ]) {
    http.on(method, pattern, ok({}));
  }
});

describe("requests", () => {
  test("createSession posts an empty object with the session deadline", async () => {
    await api.createSession();

    const [call] = http.calls;
    expect(call.url).toBe("/api/sessions");
    expect(call.options.method).toBe("POST");
    expect(call.options.body).toBe("{}");
    expect(call.options.headers).toEqual({ "Content-Type": "application/json", Accept: "application/json" });
    expect(call.deadlineMs).toBe(DEADLINES.session);
  });

  test("getSession reads one session", async () => {
    await api.getSession(SESSION_ID);

    const [call] = http.calls;
    expect(call.url).toBe(`/api/sessions/${SESSION_ID}`);
    expect(call.options.method).toBe("GET");
    expect(call.deadlineMs).toBe(DEADLINES.session);
  });

  test.each([
    ["postTurn", [{ text: "hi" }], "turns", JSON.stringify({ text: "hi" })],
    ["postBooking", [{ starts_at: "2026-11-02T09:00" }], "bookings", JSON.stringify({ starts_at: "2026-11-02T09:00" })],
    ["postVerification", [{ code: "1" }], "order-verification", JSON.stringify({ code: "1" })],
  ])("%s sends JSON to the right route with the turn deadline", async (method, args, route, body) => {
    await api[method](SESSION_ID, ...args);

    const [call] = http.calls;
    expect(call.url).toBe(`/api/sessions/${SESSION_ID}/${route}`);
    expect(call.options.method).toBe("POST");
    expect(call.options.body).toBe(body);
    expect(call.deadlineMs).toBe(DEADLINES.turn);
  });

  test("postConfirm sends the given bytes unchanged", async () => {
    await api.postConfirm(SESSION_ID, '{"request_id":"x"}');

    expect(http.calls[0].url).toBe(`/api/sessions/${SESSION_ID}/confirm`);
    expect(http.calls[0].options.body).toBe('{"request_id":"x"}');
  });

  test("postSpeech asks for audio and sends only the message id", async () => {
    await api.postSpeech(SESSION_ID, "msg-1");

    const [call] = http.calls;
    expect(call.url).toBe(`/api/sessions/${SESSION_ID}/speech`);
    expect(call.options.headers.Accept).toBe("audio/wav, audio/mpeg");
    expect(JSON.parse(call.options.body)).toEqual({ message_id: "msg-1" });
    expect(call.deadlineMs).toBe(DEADLINES.tts);
  });

  test("postTranscription uploads the file under its own name with the caller's abort controller", async () => {
    const controller = new AbortController();
    const blob = new Blob(["RIFF"], { type: "audio/wav" });

    await api.postTranscription(blob, controller);

    const [call] = http.calls;
    expect(call.url).toBe("/api/transcriptions");
    expect(call.options.body.get("file").name).toBe("recording.wav");
    expect(call.deadlineMs).toBe(DEADLINES.stt);
    expect(call.abortController).toBe(controller);
  });

  test("postTranscription keeps the name of an attached file", async () => {
    const file = new File(["RIFF"], "spoken.wav", { type: "audio/wav" });

    await api.postTranscription(file, new AbortController());

    expect(http.calls[0].options.body.get("file").name).toBe("spoken.wav");
  });
});

describe("parseError", () => {
  test("translates the error code in the current language", async () => {
    language = "es";
    const parsed = await api.parseError(failure("revision_conflict", 409, { current_revision: 7 }));

    expect(parsed.message).toBe(ES["errors.revision_conflict"]);
    expect(parsed.code).toBe("revision_conflict");
    expect(parsed.currentRevision).toBe(7);
    expect(parsed.retryable).toBe(false);
  });

  test("passes the retryable flag through", async () => {
    const parsed = await api.parseError(failure("provider_unavailable", 503, { retryable: true }));

    expect(parsed.retryable).toBe(true);
    expect(parsed.message).toBe(EN["errors.provider_unavailable"]);
  });

  test("a body without an error object falls back to the provider-unavailable message", async () => {
    const parsed = await api.parseError(new FakeResponse({ detail: "x" }, { status: 500 }));

    expect(parsed.message).toBe(EN["errors.provider_unavailable"]);
    expect(parsed.code).toBeUndefined();
    expect(parsed.retryable).toBe(false);
  });

  test("an unreadable body is treated as a retryable provider failure", async () => {
    const parsed = await api.parseError(new FakeResponse(new Error("bad json"), { status: 502 }));

    expect(parsed).toEqual({
      message: EN["errors.provider_unavailable"],
      code: null,
      retryable: true,
      currentRevision: undefined,
      data: null,
    });
  });
});

describe("transportMessage", () => {
  test("distinguishes a timeout, a missing language pack and any other failure", () => {
    const abort = Object.assign(new Error("x"), { name: "AbortError" });
    const locale = Object.assign(new Error("x"), { name: "LocaleError" });

    expect(api.transportMessage(abort)).toBe(EN["errors.provider_timeout"]);
    expect(api.transportMessage(locale)).toBe(EN["errors.locale_unavailable"]);
    expect(api.transportMessage(new Error("offline"))).toBe(EN["errors.provider_unavailable"]);
    expect(api.transportMessage(undefined)).toBe(EN["errors.provider_unavailable"]);
  });

  test("uses the current language", () => {
    language = "es";

    expect(api.transportMessage(new Error("x"))).toBe(ES["errors.provider_unavailable"]);
  });
});
