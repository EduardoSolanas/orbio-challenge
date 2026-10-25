export const LANGUAGES = Object.freeze(["en", "es"]);
export const DEFAULT_LANGUAGE = "en";

export const ERROR_KEYS = Object.freeze({
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
  too_many_verification_attempts: "errors.too_many_verification_attempts",
  backend_api_unavailable: "errors.backend_api_unavailable",
  backend_api_timeout: "errors.backend_api_timeout",
  invalid_backend_api_response: "errors.invalid_backend_api_response",
  slot_unavailable: "errors.slot_unavailable",
  already_booked: "errors.already_booked",
  verification_required: "errors.verification_required",
  internal_error: "errors.internal_error",
});

const PLACEHOLDER = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

export class LocaleError extends Error {
  constructor(message) {
    super(message);
    this.name = "LocaleError";
  }
}

/** Holds the translation tables and applies them to the page. */
export class I18n {
  constructor({
    document = globalThis.document ?? null,
    fetchFn = (...args) => globalThis.fetch(...args),
  } = {}) {
    this.document = document;
    this.fetchFn = fetchFn;
    this.tables = new Map();
  }

  static languageOf(value) {
    return value === "es" ? "es" : "en";
  }

  static isLocaleError(error) {
    return !!error && error.name === "LocaleError";
  }

  languageOf(value) {
    return I18n.languageOf(value);
  }

  isLocaleError(error) {
    return I18n.isLocaleError(error);
  }

  requireLanguage(value) {
    if (!LANGUAGES.includes(value)) {
      throw new LocaleError(`unsupported locale: ${value}`);
    }
    return value;
  }

  seed(language, mapping) {
    const validated = this.validate(mapping, this.requireLanguage(language));
    this.tables.set(language, validated);
    return validated;
  }

  validate(mapping, language) {
    if (!mapping || typeof mapping !== "object" || Array.isArray(mapping)) {
      throw new LocaleError(`locale catalog must be an object: ${language}`);
    }
    const table = {};
    for (const [key, value] of Object.entries(mapping)) {
      if (typeof value !== "string" || !value.trim()) {
        throw new LocaleError(`non-empty string required for ${language}:${key}`);
      }
      table[key] = value;
    }
    return Object.freeze(table);
  }

  /** Load the English catalogue the page starts in. */
  async init() {
    return this.load(DEFAULT_LANGUAGE);
  }

  async load(language) {
    const target = this.requireLanguage(language);
    let response;
    try {
      response = await this.fetchFn(`/api/locales/${target}`, {
        headers: { Accept: "application/json" },
        cache: "no-cache",
      });
    } catch (_) {
      throw new LocaleError(`locale request failed: ${target}`);
    }
    if (!response.ok) throw new LocaleError(`locale request failed: ${target}`);
    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      throw new LocaleError(`invalid locale response: ${target}`);
    }
    this.seed(target, payload);
    return target;
  }

  /** Make sure the table for ``language`` is loaded; resolves to the language used. */
  async ensure(language) {
    const target = this.languageOf(language);
    if (!this.has(target)) await this.load(target);
    return target;
  }

  has(language) {
    return this.tables.has(this.languageOf(language));
  }

  static format(template, values) {
    return template.replace(PLACEHOLDER, (_match, name) => {
      if (!Object.prototype.hasOwnProperty.call(values, name)) {
        throw new Error(`missing placeholder value: ${name}`);
      }
      return String(values[name]);
    });
  }

  t(language, key, values) {
    const target = this.requireLanguage(language);
    const table = this.tables.get(target);
    if (!table) throw new LocaleError(`locale not loaded: ${target}`);
    if (!Object.prototype.hasOwnProperty.call(table, key)) {
      throw new Error(`missing i18n key: ${key}`);
    }
    const template = table[key];
    return values ? I18n.format(template, values) : template;
  }

  translateError(code, language) {
    const key = Object.prototype.hasOwnProperty.call(ERROR_KEYS, code)
      ? ERROR_KEYS[code]
      : "errors.internal_error";
    return this.t(this.requireLanguage(language), key);
  }

  applyStaticLabels(language) {
    const target = this.requireLanguage(language);
    if (!this.tables.has(target)) throw new LocaleError(`locale not loaded: ${target}`);
    const doc = this.document;
    if (!doc) return target;
    doc.documentElement.lang = target;
    for (const element of doc.querySelectorAll("[data-i18n]")) {
      element.textContent = this.t(target, element.getAttribute("data-i18n"));
    }
    for (const element of doc.querySelectorAll("[data-i18n-aria-label]")) {
      element.setAttribute("aria-label", this.t(target, element.getAttribute("data-i18n-aria-label")));
    }
    for (const element of doc.querySelectorAll("[data-i18n-placeholder]")) {
      element.setAttribute("placeholder", this.t(target, element.getAttribute("data-i18n-placeholder")));
    }
    return target;
  }

  catalogKeys() {
    const keys = new Set();
    for (const table of this.tables.values()) {
      for (const key of Object.keys(table)) keys.add(key);
    }
    return [...keys].sort();
  }

  reset() {
    this.tables.clear();
  }
}
