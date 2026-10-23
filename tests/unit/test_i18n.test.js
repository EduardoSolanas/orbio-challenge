const test = require("node:test");
const assert = require("node:assert/strict");
const {
  LANGUAGES,
  STRINGS,
  ERROR_MESSAGES,
  languageOf,
  t,
  translateError,
  applyStaticLabels,
} = require("../../app/static/i18n.js");

function fakeDocument() {
  const elements = new Map();
  const make = (id) => {
    const element = {
      id,
      textContent: "",
      attributes: {},
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
    };
    elements.set(id, element);
    return element;
  };
  const disclosure = {
    textContent: "",
  };
  return {
    documentElement: { lang: "en" },
    elements,
    make,
    getElementById(id) {
      return elements.get(id) || null;
    },
    querySelector(selector) {
      return selector === ".playback__disclosure" ? disclosure : null;
    },
  };
}

test("supported languages are exactly English and Spanish", () => {
  assert.deepEqual(LANGUAGES, ["en", "es"]);
});

test("every UI key exists in both languages with non-blank copy", () => {
  assert.deepEqual(Object.keys(STRINGS.en).sort(), Object.keys(STRINGS.es).sort());
  for (const key of Object.keys(STRINGS.en)) {
    assert.ok(STRINGS.en[key].trim(), key);
    assert.ok(STRINGS.es[key].trim(), key);
  }
});

test("every backend error code has an English and Spanish translation", () => {
  const codes = [
    "session_not_found",
    "message_not_found",
    "revision_conflict",
    "request_id_conflict",
    "stale_review",
    "session_completed",
    "turn_limit",
    "review_not_ready",
    "input_too_large",
    "unsupported_audio",
    "invalid_request",
    "invalid_audio",
    "silent_audio",
    "empty_transcription",
    "transcript_too_long",
    "provider_busy",
    "session_busy",
    "invalid_provider_response",
    "provider_unavailable",
    "provider_configuration",
    "storage_unavailable",
    "storage_corrupt",
    "provider_timeout",
    "internal_error",
  ];
  for (const code of codes) {
    assert.ok(ERROR_MESSAGES.en[code], code);
    assert.ok(ERROR_MESSAGES.es[code], code);
  }
  assert.deepEqual(
    Object.keys(ERROR_MESSAGES.en).sort(),
    Object.keys(ERROR_MESSAGES.es).sort()
  );
});

test("Spanish error copy differs from English and is selected by code", () => {
  assert.equal(translateError("revision_conflict", "es"), "Esta sesión ha cambiado. Actualiza antes de volver a enviar.");
  assert.equal(translateError("revision_conflict", "en"), "This session changed. Refresh before sending again.");
  assert.equal(
    translateError("provider_configuration", "es"),
    "La reproducción de voz no está disponible en el idioma de esta respuesta. La respuesta de texto sigue disponible."
  );
});

test("unknown or missing error codes fall back to the localized generic error", () => {
  assert.equal(translateError("not_a_code", "es"), STRINGS.es.genericError);
  assert.equal(translateError(null, "en"), STRINGS.en.genericError);
});

test("t() interpolates values and rejects unknown keys", () => {
  assert.equal(t("en", "playReplyNumbered", { number: 2 }), "Play reply 2");
  assert.equal(t("es", "playReplyNumbered", { number: 2 }), "Reproducir respuesta 2");
  assert.equal(
    t("es", "knowledgeBaseVersion", { version: "2026-10-22" }),
    "Versión de la base de conocimiento: 2026-10-22"
  );
  assert.throws(() => t("en", "not_a_key"), /missing i18n key/);
});

test("languageOf defaults any non-Spanish value to English", () => {
  assert.equal(languageOf("es"), "es");
  assert.equal(languageOf("en"), "en");
  assert.equal(languageOf("fr"), "en");
  assert.equal(languageOf(undefined), "en");
});

test("applyStaticLabels switches the document language and localizes buttons and accessible names", () => {
  const doc = fakeDocument();
  for (const id of [
    "app-title",
    "review",
    "message",
    "message-label",
    "transcribe-help",
    "send",
    "retry",
    "confirm",
    "new-intake",
    "start-recording",
    "stop-recording",
    "transcribe",
    "wav-label",
    "wav-recording",
  ]) {
    doc.make(id);
  }

  assert.equal(applyStaticLabels("es", doc), "es");
  assert.equal(doc.documentElement.lang, "es");
  assert.equal(doc.getElementById("app-title").textContent, "Registro de soporte de Northstar");
  assert.equal(doc.getElementById("send").textContent, "Enviar");
  assert.equal(doc.getElementById("confirm").textContent, "Confirmar datos");
  assert.equal(doc.getElementById("start-recording").textContent, "Empezar a grabar");
  assert.equal(doc.getElementById("message").attributes["aria-label"], "Mensaje");
  assert.equal(doc.getElementById("review").attributes["aria-label"], "Revisar datos");
  assert.equal(doc.querySelector(".playback__disclosure").textContent, "La voz es generada por IA");

  assert.equal(applyStaticLabels("en", doc), "en");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(doc.getElementById("send").textContent, "Send");
});
