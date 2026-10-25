import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { jest } from "@jest/globals";

import { App } from "../../../web/js/app.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const readJson = (relative) => JSON.parse(readFileSync(path.join(ROOT, relative), "utf8"));

export const EN = readJson("gateway/locales/en.json");
export const ES = readJson("gateway/locales/es.json");
const PAGE = readFileSync(path.join(ROOT, "web/index.html"), "utf8");

export const SESSION_ID = "11111111-1111-4111-8111-111111111111";
export const SECOND_SESSION_ID = "22222222-2222-4222-8222-222222222222";

/** Replace the jsdom body with the real index.html markup (no scripts run). */
export function loadPage(doc = document) {
  const body = /<body>([\s\S]*)<\/body>/
    .exec(PAGE)[1]
    .replace(/<script[^>]*src=[^>]*><\/script>/g, "");
  doc.body.innerHTML = body;
  doc.documentElement.lang = "en";
  return doc;
}

export class FakeResponse {
  constructor(body = {}, { status = 200, blob = null } = {}) {
    this.status = status;
    this.ok = status >= 200 && status < 300;
    this.body = body;
    this.blobValue = blob;
  }

  async json() {
    if (this.body instanceof Error) throw this.body;
    return this.body;
  }

  async blob() {
    return this.blobValue;
  }
}

export const ok = (body = {}) => new FakeResponse(body);
export const failure = (code, status, extra = {}) =>
  new FakeResponse({ error: { code, retryable: false, ...extra } }, { status });
export const audio = (size = 8, type = "audio/mpeg") =>
  new FakeResponse({}, { blob: { size, type } });

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * A stand-in for DeadlineHttp. Routes are matched on method and URL; each route returns its
 * queued responses in order and repeats the last one. A response may be a function, an Error
 * (thrown) or a promise.
 */
export class ScriptedHttp {
  constructor() {
    this.routes = [];
    this.calls = [];
  }

  /** Script a route; scripting the same method and URL again replaces the earlier script. */
  on(method, pattern, ...responses) {
    this.routes = this.routes.filter((r) => !(r.method === method && String(r.pattern) === String(pattern)));
    this.routes.push({ method, pattern, queue: responses });
    return this;
  }

  async fetchWithDeadline(url, options = {}, deadlineMs, abortController) {
    const method = options.method || "GET";
    this.calls.push({ method, url, options, deadlineMs, abortController });
    const route = this.routes.find(
      (r) => r.method === method && (r.pattern instanceof RegExp ? r.pattern.test(url) : r.pattern === url)
    );
    if (!route) throw new Error(`unexpected request ${method} ${url}`);
    const next = route.queue.length > 1 ? route.queue.shift() : route.queue[0];
    const value = typeof next === "function" ? next({ url, options, abortController }) : next;
    if (value instanceof Error) throw value;
    return value;
  }

  to(method, pattern) {
    return this.calls.filter(
      (c) => c.method === method && (pattern instanceof RegExp ? pattern.test(c.url) : c.url === pattern)
    );
  }
}

export function sessionView(overrides = {}) {
  return {
    session_id: SESSION_ID,
    revision: 0,
    status: "collecting",
    reply_language: "en",
    frustration_active: false,
    slots: { order_number: null, category: null, description: null, urgency: null },
    messages: [
      {
        id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        role: "assistant",
        text: "Hi, welcome to Northstar Energy. How can I help you today?",
        input_mode: "system",
        language: "en",
        citations: [],
        speech_available: true,
      },
    ],
    pending_clarifications: {},
    pending_review_revision: null,
    summary: null,
    ...overrides,
  };
}

export function turnResponse(overrides = {}) {
  return {
    session_id: SESSION_ID,
    revision: 1,
    status: "collecting",
    reply_language: "en",
    reply: "Which area do you need help with?",
    message_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    pending_clarifications: {},
    pending_review_revision: null,
    summary: null,
    citations: [],
    order_verification_required: false,
    slot_options: [],
    speech_available: true,
    ...overrides,
  };
}

/** The locale endpoint, as the browser's fetch would reach it. */
export function installLocaleFetch(win = window) {
  const catalogues = { en: EN, es: ES };
  win.fetch = jest.fn(async (url) => {
    const language = /\/api\/locales\/(\w+)$/.exec(url)?.[1];
    return language in catalogues ? ok(catalogues[language]) : new FakeResponse({}, { status: 404 });
  });
}

/** Stand-ins for the media APIs jsdom does not implement. */
export function installMediaStubs(win = window) {
  win.HTMLMediaElement.prototype.play = jest.fn(() => Promise.resolve());
  win.HTMLMediaElement.prototype.pause = jest.fn();
  win.HTMLMediaElement.prototype.load = jest.fn();
  win.URL.createObjectURL = jest.fn(() => "blob:reply");
  win.URL.revokeObjectURL = jest.fn();
}

/** A started App on the real page markup with a scripted server. */
export async function startApp({ http, storedId = null, newRequestId } = {}) {
  loadPage();
  window.localStorage.clear();
  if (storedId) window.localStorage.setItem("northstar.session_id", storedId);
  installMediaStubs();
  installLocaleFetch();
  let counter = 0;
  const app = new App({
    window,
    http,
    newRequestId: newRequestId ?? (() => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`),
  });
  await app.start();
  return app;
}

/** A page-level harness without starting the conversation. */
export function buildApp({ http = new ScriptedHttp(), newRequestId } = {}) {
  loadPage();
  window.localStorage.clear();
  installMediaStubs();
  let counter = 0;
  const app = new App({
    window,
    http,
    newRequestId: newRequestId ?? (() => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`),
  });
  app.i18n.seed("en", EN);
  app.i18n.seed("es", ES);
  return app;
}

/** Let queued promise callbacks run. */
export const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
