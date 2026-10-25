import { beforeEach, describe, expect, jest, test } from "@jest/globals";

import {
  DEFAULT_LANGUAGE,
  ERROR_KEYS,
  I18n,
  LANGUAGES,
  LocaleError,
} from "../../app/static/js/i18n.js";
import { EN, ES } from "./support/page.js";

const placeholders = (template) =>
  [...template.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((m) => m[1]).sort();

/** A page holding the given elements; ``bootstrap`` adds the server-rendered catalogue. */
function pageWith(html, { bootstrap = true } = {}) {
  document.body.innerHTML =
    (bootstrap ? `<script type="application/json" id="bootstrap-locale">${JSON.stringify(EN)}</script>` : "") +
    html;
}

let i18n;

beforeEach(() => {
  document.documentElement.lang = "";
  i18n = new I18n({ document });
});

describe("catalogue", () => {
  test("supported languages are exactly English and Spanish", () => {
    expect([...LANGUAGES]).toEqual(["en", "es"]);
    expect(DEFAULT_LANGUAGE).toBe("en");
  });

  test("a new translator ships no dictionaries", () => {
    expect(i18n.catalogKeys()).toEqual([]);
  });

  test("shipped dictionaries have identical keys and placeholder names", () => {
    expect(Object.keys(EN).sort()).toEqual(Object.keys(ES).sort());
    for (const key of Object.keys(EN)) {
      expect(typeof EN[key]).toBe("string");
      expect(EN[key].trim()).not.toBe("");
      expect(ES[key].trim()).not.toBe("");
      expect(placeholders(ES[key])).toEqual(placeholders(EN[key]));
    }
  });

  test("every error code maps to a key that exists in both dictionaries", () => {
    for (const key of Object.values(ERROR_KEYS)) {
      expect(EN).toHaveProperty([key]);
      expect(ES).toHaveProperty([key]);
    }
  });

  test("catalogKeys lists the union of loaded keys, sorted", () => {
    i18n.seed("en", { b: "B", a: "A" });
    i18n.seed("es", { c: "C", a: "A" });

    expect(i18n.catalogKeys()).toEqual(["a", "b", "c"]);
    i18n.reset();
    expect(i18n.catalogKeys()).toEqual([]);
  });
});

describe("init and static labels", () => {
  test("init seeds English from the server-rendered bootstrap catalogue", () => {
    pageWith('<h1 id="app-title" data-i18n="title"></h1>');

    expect(i18n.init()).toBe("en");
    expect(i18n.has("en")).toBe(true);
    expect(i18n.t("en", "actions.send")).toBe("Send");
    expect(i18n.applyStaticLabels("en")).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    expect(document.getElementById("app-title").textContent).toBe(EN.title);
  });

  test("init fails loudly without a bootstrap catalogue instead of showing raw keys", () => {
    pageWith("", { bootstrap: false });

    expect(() => i18n.init()).toThrow(/bootstrap locale/i);
    expect(i18n.has("en")).toBe(false);
  });

  test("init fails loudly when the bootstrap catalogue is not JSON", () => {
    document.body.innerHTML = '<script type="application/json" id="bootstrap-locale">not json</script>';

    expect(() => i18n.init()).toThrow(/invalid bootstrap locale/i);
  });

  test("init without a document fails the same way", () => {
    expect(() => new I18n({ document: null }).init()).toThrow(LocaleError);
  });

  test("applyStaticLabels localises placeholder attributes with the active locale", () => {
    pageWith('<input id="composer" data-i18n-placeholder="actions.send">');
    i18n.init();

    i18n.applyStaticLabels("en");
    expect(document.getElementById("composer").getAttribute("placeholder")).toBe("Send");
    i18n.seed("es", ES);
    i18n.applyStaticLabels("es");
    expect(document.getElementById("composer").getAttribute("placeholder")).toBe("Enviar");
  });

  test("applyStaticLabels updates document lang, visible text and accessible names", () => {
    pageWith(`
      <h1 id="app-title" data-i18n="title"></h1>
      <button id="send" data-i18n="actions.send"></button>
      <button id="confirm" data-i18n="actions.confirm"></button>
      <textarea id="message" data-i18n-aria-label="ui.message_label"></textarea>
      <section id="review" data-i18n-aria-label="ui.review_region"></section>
      <p id="disclosure" data-i18n="speech.disclosure"></p>`);
    i18n.init();
    i18n.seed("es", ES);

    expect(i18n.applyStaticLabels("es")).toBe("es");
    expect(document.documentElement.lang).toBe("es");
    expect(document.getElementById("app-title").textContent).toBe(ES.title);
    expect(document.getElementById("send").textContent).toBe("Enviar");
    expect(document.getElementById("confirm").textContent).toBe(ES["actions.confirm"]);
    expect(document.getElementById("message").getAttribute("aria-label")).toBe(ES["ui.message_label"]);
    expect(document.getElementById("review").getAttribute("aria-label")).toBe(ES["ui.review_region"]);
    expect(document.getElementById("disclosure").textContent).toBe(ES["speech.disclosure"]);

    expect(i18n.applyStaticLabels("en")).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    expect(document.getElementById("send").textContent).toBe("Send");
  });

  test("applyStaticLabels refuses an unloaded locale instead of mixing languages", () => {
    pageWith('<button id="send" data-i18n="actions.send"></button>');
    i18n.init();

    expect(() => i18n.applyStaticLabels("es")).toThrow(/locale not loaded/);
    expect(document.getElementById("send").textContent).toBe("");
  });

  test("applyStaticLabels is a no-op without a document", () => {
    const detached = new I18n({ document: null });
    detached.seed("en", EN);

    expect(detached.applyStaticLabels("en")).toBe("en");
  });
});

describe("translation", () => {
  beforeEach(() => {
    i18n.seed("en", EN);
    i18n.seed("es", ES);
  });

  test("t() interpolates named placeholders and rejects unknown keys or locales", () => {
    expect(i18n.t("en", "speech.play", { ordinal: 2 })).toBe("Play reply 2");
    expect(i18n.t("es", "speech.play", { ordinal: 2 })).toBe("Reproducir respuesta 2");
    expect(i18n.t("es", "ui.knowledge_base_version", { version: "2026-10-25" })).toBe(
      "Versión de la base de conocimiento: 2026-10-25"
    );
    expect(() => i18n.t("en", "not_a_key")).toThrow(/missing i18n key/);
    expect(() => i18n.t("fr", "actions.send")).toThrow(/locale/);
    expect(() => i18n.t("en", "speech.play", {})).toThrow(/placeholder/);
  });

  test("t() refuses a supported locale whose table was never loaded", () => {
    const fresh = new I18n({ document });
    fresh.seed("en", EN);

    expect(() => fresh.t("es", "actions.send")).toThrow(/locale not loaded/);
  });

  test("t() treats placeholder values as literal text, never templates", () => {
    expect(i18n.t("en", "receipt", { session_id: "{other.__class__}" })).toBe(
      "Saved locally as {other.__class__}. No request has been sent to a real support team."
    );
    expect(i18n.t("en", "speech.play", { ordinal: "<b>&</b>" })).toBe("Play reply <b>&</b>");
  });

  test("translateError maps canonical error codes to localised keys", () => {
    expect(i18n.translateError("revision_conflict", "es")).toBe(ES["errors.revision_conflict"]);
    expect(i18n.translateError("provider_configuration", "es")).toBe(ES["speech.unavailable"]);
    expect(i18n.translateError("backend_api_unavailable", "en")).toBe(EN["errors.backend_api_unavailable"]);
    expect(i18n.translateError("slot_unavailable", "en")).toBe(EN["errors.slot_unavailable"]);
    expect(i18n.translateError("already_booked", "es")).toBe(ES["errors.already_booked"]);
    expect(i18n.translateError("verification_required", "en")).toBe(EN["errors.verification_required"]);
  });

  test("unknown or missing error codes fall back to the shared generic error", () => {
    expect(i18n.translateError("not_a_code", "es")).toBe(ES["errors.internal_error"]);
    expect(i18n.translateError(null, "en")).toBe(EN["errors.internal_error"]);
  });

  test("languageOf defaults any non-Spanish value to English", () => {
    expect(i18n.languageOf("es")).toBe("es");
    expect(i18n.languageOf("en")).toBe("en");
    expect(i18n.languageOf("fr")).toBe("en");
    expect(i18n.languageOf(undefined)).toBe("en");
    expect(I18n.languageOf("es")).toBe("es");
  });

  test("seed() rejects blank or non-string catalogue values", () => {
    const fresh = new I18n({ document });

    expect(() => fresh.seed("en", { greeting: "  " })).toThrow(/non-empty string/);
    expect(() => fresh.seed("en", { greeting: 1 })).toThrow(/non-empty string/);
    expect(() => fresh.seed("en", ["greeting"])).toThrow(/object/);
    expect(() => fresh.seed("en", null)).toThrow(/object/);
    expect(() => fresh.seed("fr", { a: "b" })).toThrow(/unsupported locale/);
  });

  test("a seeded table cannot be changed afterwards", () => {
    const table = i18n.seed("en", { a: "A" });

    expect(Object.isFrozen(table)).toBe(true);
  });
});

describe("loading", () => {
  test("load() fetches only the selected dictionary and seeds it", async () => {
    const fetchFn = jest.fn(async () => ({ ok: true, status: 200, json: async () => ES }));
    const loader = new I18n({ document, fetchFn });

    expect(await loader.load("es")).toBe("es");
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][0]).toBe("/api/locales/es");
    expect(fetchFn.mock.calls[0][1].cache).toBe("no-cache");
    expect(loader.has("es")).toBe(true);
    expect(loader.t("es", "actions.send")).toBe("Enviar");
  });

  test("load() rejects a failed, unreachable or malformed response with a LocaleError", async () => {
    const attempts = [
      async () => ({ ok: false, status: 404, json: async () => ({}) }),
      async () => {
        throw new Error("offline");
      },
      async () => ({ ok: true, status: 200, json: async () => ({ send: 1 }) }),
      async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error("not json");
        },
      }),
    ];
    for (const fetchFn of attempts) {
      const loader = new I18n({ document, fetchFn });

      await expect(loader.load("es")).rejects.toBeInstanceOf(LocaleError);
      expect(loader.has("es")).toBe(false);
    }
  });

  test("load() refuses an unsupported language without calling the network", async () => {
    const fetchFn = jest.fn();

    await expect(new I18n({ document, fetchFn }).load("fr")).rejects.toThrow(/unsupported locale/);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  test("ensure() loads a missing language once and reuses it", async () => {
    const fetchFn = jest.fn(async () => ({ ok: true, status: 200, json: async () => ES }));
    const loader = new I18n({ document, fetchFn });

    expect(await loader.ensure("es")).toBe("es");
    expect(await loader.ensure("es")).toBe("es");
    // An unknown language falls back to English, which is not loaded yet either.
    expect(await loader.ensure("fr")).toBe("en");
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  test("isLocaleError recognises only LocaleError", () => {
    expect(i18n.isLocaleError(new LocaleError("x"))).toBe(true);
    expect(i18n.isLocaleError(new Error("x"))).toBe(false);
    expect(i18n.isLocaleError(null)).toBe(false);
  });
});
