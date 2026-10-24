(() => {
  "use strict";

  const LANGUAGES = Object.freeze(["en", "es"]);
  const DEFAULT_LANGUAGE = "en";
  const tables = new Map();

  const ERROR_KEYS = Object.freeze({
    session_not_found: "errors.session_not_found",
    message_not_found: "errors.message_not_found",
    revision_conflict: "errors.revision_conflict",
    request_id_conflict: "errors.request_id_conflict",
    stale_review: "errors.stale_review",
    session_completed: "errors.session_completed",
    turn_limit: "errors.turn_limit",
    review_not_ready: "errors.review_not_ready",
    input_too_large: "errors.input_too_large",
    unsupported_audio: "errors.unsupported_audio",
    invalid_request: "errors.invalid_request",
    invalid_audio: "errors.invalid_audio",
    silent_audio: "errors.silent_audio",
    empty_transcription: "errors.empty_transcription",
    transcript_too_long: "errors.transcript_too_long",
    provider_busy: "errors.provider_busy",
    session_busy: "errors.session_busy",
    invalid_provider_response: "errors.invalid_provider_response",
    provider_unavailable: "errors.provider_unavailable",
    provider_configuration: "speech.unavailable",
    storage_unavailable: "errors.storage_unavailable",
    storage_corrupt: "errors.storage_corrupt",
    provider_timeout: "errors.provider_timeout",
    internal_error: "errors.internal_error",
  });

  function languageOf(value) {
    return value === "es" ? "es" : "en";
  }

  function requireLanguage(value) {
    if (!LANGUAGES.includes(value)) {
      throw localeError(`unsupported locale: ${value}`);
    }
    return value;
  }

  function localeError(message) {
    const error = new Error(message);
    error.name = "LocaleError";
    return error;
  }

  function isLocaleError(error) {
    return !!error && error.name === "LocaleError";
  }

  function validate(mapping, language) {
    if (!mapping || typeof mapping !== "object" || Array.isArray(mapping)) {
      throw localeError(`locale catalog must be an object: ${language}`);
    }
    const table = {};
    for (const [key, value] of Object.entries(mapping)) {
      if (typeof value !== "string" || !value.trim()) {
        throw localeError(`non-empty string required for ${language}:${key}`);
      }
      table[key] = value;
    }
    return Object.freeze(table);
  }

  function seed(language, mapping) {
    const validated = validate(mapping, requireLanguage(language));
    tables.set(language, validated);
    return validated;
  }

  function init(doc) {
    const documentRef = doc || (typeof document !== "undefined" ? document : null);
    const element =
      documentRef && documentRef.getElementById("bootstrap-locale");
    if (!element) throw localeError("missing bootstrap locale catalog");
    let parsed;
    try {
      parsed = JSON.parse(element.textContent || "");
    } catch (_) {
      throw localeError("invalid bootstrap locale catalog");
    }
    seed(DEFAULT_LANGUAGE, parsed);
    return DEFAULT_LANGUAGE;
  }

  async function load(language) {
    const target = requireLanguage(language);
    let response;
    try {
      response = await fetch(`/api/locales/${target}`, {
        headers: { Accept: "application/json" },
        cache: "no-cache",
      });
    } catch (_) {
      throw localeError(`locale request failed: ${target}`);
    }
    if (!response.ok) throw localeError(`locale request failed: ${target}`);
    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      throw localeError(`invalid locale response: ${target}`);
    }
    seed(target, payload);
    return target;
  }

  function has(language) {
    return tables.has(languageOf(language));
  }

  function format(template, values) {
    return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (match, name) => {
      if (!Object.prototype.hasOwnProperty.call(values, name)) {
        throw new Error(`missing placeholder value: ${name}`);
      }
      return String(values[name]);
    });
  }

  function t(language, key, values) {
    const target = requireLanguage(language);
    const table = tables.get(target);
    if (!table) throw localeError(`locale not loaded: ${target}`);
    if (!Object.prototype.hasOwnProperty.call(table, key)) {
      throw new Error(`missing i18n key: ${key}`);
    }
    const template = table[key];
    return values ? format(template, values) : template;
  }

  function translateError(code, language) {
    const key = Object.prototype.hasOwnProperty.call(ERROR_KEYS, code)
      ? ERROR_KEYS[code]
      : "errors.internal_error";
    return t(requireLanguage(language), key);
  }

  function applyStaticLabels(language, doc) {
    const target = requireLanguage(language);
    if (!tables.has(target)) throw localeError(`locale not loaded: ${target}`);
    const documentRef = doc || (typeof document !== "undefined" ? document : null);
    if (!documentRef) return target;
    documentRef.documentElement.lang = target;
    for (const element of documentRef.querySelectorAll("[data-i18n]")) {
      element.textContent = t(target, element.getAttribute("data-i18n"));
    }
    for (const element of documentRef.querySelectorAll("[data-i18n-aria-label]")) {
      element.setAttribute(
        "aria-label",
        t(target, element.getAttribute("data-i18n-aria-label"))
      );
    }
    return target;
  }

  function catalogKeys() {
    const keys = new Set();
    for (const table of tables.values()) {
      for (const key of Object.keys(table)) keys.add(key);
    }
    return [...keys].sort();
  }

  function reset() {
    tables.clear();
  }

  const api = Object.freeze({
    LANGUAGES,
    DEFAULT_LANGUAGE,
    languageOf,
    requireLanguage,
    localeError,
    isLocaleError,
    seed,
    init,
    load,
    has,
    t,
    translateError,
    applyStaticLabels,
    catalogKeys,
    reset,
  });

  if (typeof document !== "undefined") {
    init(document);
    applyStaticLabels(DEFAULT_LANGUAGE, document);
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.NorthstarI18n = api;
})();
