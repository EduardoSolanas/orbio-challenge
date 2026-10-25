import { DEADLINES } from "./config.js";

const JSON_HEADERS = Object.freeze({
  "Content-Type": "application/json",
  Accept: "application/json",
});

/** Every request the page makes to the gateway, with its deadline and headers. */
export class ApiClient {
  constructor({ http, i18n, getLanguage }) {
    this.http = http;
    this.i18n = i18n;
    this.getLanguage = getLanguage;
  }

  createSession() {
    return this.http.fetchWithDeadline(
      "/api/sessions",
      { method: "POST", headers: { ...JSON_HEADERS }, body: "{}" },
      DEADLINES.session
    );
  }

  getSession(sessionId) {
    return this.http.fetchWithDeadline(
      `/api/sessions/${sessionId}`,
      { method: "GET", headers: { Accept: "application/json" } },
      DEADLINES.session
    );
  }

  postTurn(sessionId, payload) {
    return this.postJson(`/api/sessions/${sessionId}/turns`, JSON.stringify(payload));
  }

  /** ``body`` is the exact serialized command, so a retry sends identical bytes. */
  postConfirm(sessionId, body) {
    return this.postJson(`/api/sessions/${sessionId}/confirm`, body);
  }

  postBooking(sessionId, payload) {
    return this.postJson(`/api/sessions/${sessionId}/bookings`, JSON.stringify(payload));
  }

  postVerification(sessionId, payload) {
    return this.postJson(`/api/sessions/${sessionId}/order-verification`, JSON.stringify(payload));
  }

  postSpeech(sessionId, messageId) {
    return this.http.fetchWithDeadline(
      `/api/sessions/${sessionId}/speech`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "audio/wav, audio/mpeg" },
        body: JSON.stringify({ message_id: messageId }),
      },
      DEADLINES.tts
    );
  }

  postTranscription(blob, abortController) {
    const body = new FormData();
    body.append("file", blob, blob.name || "recording.wav");
    return this.http.fetchWithDeadline(
      "/api/transcriptions",
      { method: "POST", body },
      DEADLINES.stt,
      abortController
    );
  }

  postJson(url, body) {
    return this.http.fetchWithDeadline(
      url,
      { method: "POST", headers: { ...JSON_HEADERS }, body },
      DEADLINES.turn
    );
  }

  ui(key) {
    return this.i18n.t(this.getLanguage(), key);
  }

  /** Turn a failed response into a localised message plus the fields callers branch on. */
  async parseError(response) {
    try {
      const data = await response.json();
      const error = data && data.error;
      const code = error && error.code;
      return {
        message: code
          ? this.i18n.translateError(code, this.getLanguage())
          : this.ui("errors.provider_unavailable"),
        code,
        retryable: !!(error && error.retryable),
        currentRevision: error && error.current_revision,
        data,
      };
    } catch (_) {
      return {
        message: this.ui("errors.provider_unavailable"),
        code: null,
        retryable: true,
        currentRevision: undefined,
        data: null,
      };
    }
  }

  /** The message for a request that never produced a response. */
  transportMessage(error) {
    if (this.i18n.isLocaleError(error)) return this.ui("errors.locale_unavailable");
    return error && error.name === "AbortError"
      ? this.ui("errors.provider_timeout")
      : this.ui("errors.provider_unavailable");
  }
}
