const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");
const i18n = require("../../app/static/i18n.js");

const LOCALES = path.join(__dirname, "..", "..", "app", "locales");
const en = JSON.parse(fs.readFileSync(path.join(LOCALES, "en.json"), "utf8"));
const es = JSON.parse(fs.readFileSync(path.join(LOCALES, "es.json"), "utf8"));

function placeholders(template) {
  return [...template.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((m) => m[1]).sort();
}

function fakeDocument(definitions) {
  const byId = new Map();
  const all = [];
  for (const [id, attributes] of Object.entries(definitions)) {
    const element = {
      id,
      textContent: "",
      attributes: { ...attributes },
      getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name)
          ? this.attributes[name]
          : null;
      },
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
    };
    byId.set(id, element);
    all.push(element);
  }
  return {
    documentElement: { lang: "en" },
    getElementById(id) {
      return byId.get(id) || null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-i18n]") {
        return all.filter((el) => el.getAttribute("data-i18n") !== null);
      }
      if (selector === "[data-i18n-aria-label]") {
        return all.filter((el) => el.getAttribute("data-i18n-aria-label") !== null);
      }
      if (selector === "[data-i18n-placeholder]") {
        return all.filter((el) => el.getAttribute("data-i18n-placeholder") !== null);
      }
      return [];
    },
  };
}

function bootstrapDocument(definitions, locale = en) {
  const doc = fakeDocument(definitions);
  doc.getElementById("bootstrap-locale").textContent = JSON.stringify(locale);
  return doc;
}

function reset() {
  i18n.reset();
}

test("supported languages are exactly English and Spanish", () => {
  assert.deepEqual([...i18n.LANGUAGES], ["en", "es"]);
  assert.equal(i18n.DEFAULT_LANGUAGE, "en");
});

test("the browser module ships no duplicate dictionaries", () => {
  assert.equal(i18n.STRINGS, undefined);
  assert.equal(i18n.ERROR_MESSAGES, undefined);
  assert.deepEqual(i18n.catalogKeys(), []);
});

test("shipped dictionaries have identical keys and placeholder names", () => {
  assert.deepEqual(Object.keys(en).sort(), Object.keys(es).sort());
  for (const key of Object.keys(en)) {
    assert.equal(typeof en[key], "string", key);
    assert.ok(en[key].trim(), key);
    assert.ok(es[key].trim(), key);
    assert.deepEqual(placeholders(en[key]), placeholders(es[key]), key);
  }
});

test("init seeds English from the server-rendered bootstrap catalog", () => {
  reset();
  const doc = bootstrapDocument({
    "bootstrap-locale": {},
    "app-title": { "data-i18n": "title" },
  });

  assert.equal(i18n.init(doc), "en");
  assert.equal(i18n.has("en"), true);
  assert.equal(i18n.t("en", "actions.send"), "Send");
  assert.equal(i18n.applyStaticLabels("en", doc), "en");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(doc.getElementById("app-title").textContent, en.title);
});

test("init fails loudly without a bootstrap catalog instead of showing raw keys", () => {
  reset();
  const doc = fakeDocument({});
  assert.throws(() => i18n.init(doc), /bootstrap locale/i);
  assert.equal(i18n.has("en"), false);
});

test("applyStaticLabels localizes placeholder attributes with the active locale", () => {
  reset();
  const doc = bootstrapDocument({
    "bootstrap-locale": {},
    composer: { "data-i18n-placeholder": "actions.send" },
  });

  i18n.init(doc);
  i18n.applyStaticLabels("en", doc);
  assert.equal(doc.getElementById("composer").getAttribute("placeholder"), "Send");

  i18n.seed("es", es);
  i18n.applyStaticLabels("es", doc);
  assert.equal(doc.getElementById("composer").getAttribute("placeholder"), "Enviar");
});

test("t() interpolates named placeholders and rejects unknown keys or locales", () => {
  reset();
  i18n.seed("en", en);
  i18n.seed("es", es);

  assert.equal(i18n.t("en", "speech.play", { ordinal: 2 }), "Play reply 2");
  assert.equal(i18n.t("es", "speech.play", { ordinal: 2 }), "Reproducir respuesta 2");
  assert.equal(
    i18n.t("es", "ui.knowledge_base_version", { version: "2026-10-22" }),
    "Versión de la base de conocimiento: 2026-10-22"
  );
  assert.throws(() => i18n.t("en", "not_a_key"), /missing i18n key/);
  assert.throws(() => i18n.t("fr", "actions.send"), /locale/);
  assert.throws(() => i18n.t("en", "speech.play", {}), /placeholder/);
});

test("t() treats placeholder values as literal text, never templates", () => {
  reset();
  i18n.seed("en", en);

  assert.equal(
    i18n.t("en", "receipt", { session_id: "{other.__class__}" }),
    "Saved locally as {other.__class__}. No request has been sent to a real support team."
  );
  assert.equal(
    i18n.t("en", "speech.play", { ordinal: "<b>&</b>" }),
    "Play reply <b>&</b>"
  );
});

test("translateError maps canonical error codes to localized keys", () => {
  reset();
  i18n.seed("en", en);
  i18n.seed("es", es);

  assert.equal(
    i18n.translateError("revision_conflict", "es"),
    es["errors.revision_conflict"]
  );
  assert.equal(
    i18n.translateError("provider_unavailable", "en"),
    en["errors.provider_unavailable"]
  );
  assert.equal(
    i18n.translateError("provider_configuration", "es"),
    es["speech.unavailable"]
  );
  assert.equal(
    i18n.translateError("backend_api_unavailable", "en"),
    en["errors.backend_api_unavailable"]
  );
  assert.equal(
    i18n.translateError("backend_api_timeout", "es"),
    es["errors.backend_api_timeout"]
  );
  assert.equal(
    i18n.translateError("invalid_backend_api_response", "en"),
    en["errors.invalid_backend_api_response"]
  );
});

test("unknown or missing error codes fall back to the shared generic error", () => {
  reset();
  i18n.seed("en", en);
  i18n.seed("es", es);

  assert.equal(i18n.translateError("not_a_code", "es"), es["errors.internal_error"]);
  assert.equal(i18n.translateError(null, "en"), en["errors.internal_error"]);
});

test("languageOf defaults any non-Spanish value to English", () => {
  assert.equal(i18n.languageOf("es"), "es");
  assert.equal(i18n.languageOf("en"), "en");
  assert.equal(i18n.languageOf("fr"), "en");
  assert.equal(i18n.languageOf(undefined), "en");
});

test("applyStaticLabels updates document lang, visible text and accessible names", () => {
  reset();
  const doc = bootstrapDocument({
    "bootstrap-locale": {},
    "app-title": { "data-i18n": "title" },
    send: { "data-i18n": "actions.send" },
    confirm: { "data-i18n": "actions.confirm" },
    "start-recording": { "data-i18n": "actions.start_recording" },
    message: { "data-i18n-aria-label": "ui.message_label" },
    review: { "data-i18n-aria-label": "ui.review_region" },
    "wav-recording": { "data-i18n-aria-label": "actions.choose_wav" },
    disclosure: { "data-i18n": "speech.disclosure" },
  });
  i18n.init(doc);
  i18n.seed("es", es);

  assert.equal(i18n.applyStaticLabels("es", doc), "es");
  assert.equal(doc.documentElement.lang, "es");
  assert.equal(doc.getElementById("app-title").textContent, es.title);
  assert.equal(doc.getElementById("send").textContent, "Enviar");
  assert.equal(doc.getElementById("confirm").textContent, es["actions.confirm"]);
  assert.equal(
    doc.getElementById("start-recording").textContent,
    es["actions.start_recording"]
  );
  assert.equal(doc.getElementById("message").getAttribute("aria-label"), es["ui.message_label"]);
  assert.equal(doc.getElementById("review").getAttribute("aria-label"), es["ui.review_region"]);
  assert.equal(
    doc.getElementById("wav-recording").getAttribute("aria-label"),
    es["actions.choose_wav"]
  );
  assert.equal(doc.getElementById("disclosure").textContent, es["speech.disclosure"]);

  assert.equal(i18n.applyStaticLabels("en", doc), "en");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(doc.getElementById("send").textContent, "Send");
});

test("applyStaticLabels refuses an unloaded locale instead of mixing languages", () => {
  reset();
  const doc = bootstrapDocument({
    "bootstrap-locale": {},
    send: { "data-i18n": "actions.send" },
  });
  i18n.init(doc);

  assert.throws(() => i18n.applyStaticLabels("es", doc), /locale/);
  assert.equal(doc.getElementById("send").textContent, "");
});

test("load() fetches only the selected dictionary and seeds it", async () => {
  reset();
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, status: 200, json: async () => es };
  };

  assert.equal(await i18n.load("es"), "es");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/locales/es");
  assert.equal(calls[0].options.cache, "no-cache");
  assert.equal(i18n.has("es"), true);
  assert.equal(i18n.t("es", "actions.send"), "Enviar");
});

test("load() rejects a failed or malformed locale response with a LocaleError", async () => {
  reset();
  globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
  await assert.rejects(i18n.load("es"), (error) => i18n.isLocaleError(error));
  assert.equal(i18n.has("es"), false);

  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ send: 1 }) });
  await assert.rejects(i18n.load("es"), (error) => i18n.isLocaleError(error));
  assert.equal(i18n.has("es"), false);
});

test("seed() rejects blank or non-string catalog values", () => {
  reset();
  assert.throws(() => i18n.seed("en", { greeting: "  " }), /non-empty string/);
  assert.throws(() => i18n.seed("en", { greeting: 1 }), /non-empty string/);
  assert.throws(() => i18n.seed("en", ["greeting"]), /object/);
});
