(() => {
  "use strict";

  const app = (window.NorthstarApp = {});

  const STORAGE_KEY = "northstar.session_id";

  const MAX_CHARS = 4000;

  const MAX_WAV_BYTES = 10 * 1024 * 1024;

  const MAX_RECORD_SECONDS = 60;

  const DEADLINE_SESSION_MS = 5000;

  const DEADLINE_TURN_MS = 35000;

  const DEADLINE_STT_MS = 45000;

  const DEADLINE_TTS_MS = 30000;

  const i18n = window.NorthstarI18n;

  function ui(key, values) {
    return i18n.t(state.language, key, values);
  }

  async function ensureLocale(language) {
    const target = i18n.languageOf(language);
    if (!i18n.has(target)) await i18n.load(target);
    return target;
  }

  function speechLabel(playable, pending, ordinal) {
    if (!playable) return ui("speech.unavailable");
    if (pending) return ui("speech.loading");
    return ui("speech.play", { ordinal });
  }

  const els = {
    log: document.getElementById("transcript"),
    review: document.getElementById("review"),
    slotPicker: document.getElementById("slot-picker"),
    alert: document.getElementById("alert"),
    verification: document.getElementById("order-verification"),
    verificationNotice: document.getElementById("verification-notice"),
    verificationError: document.getElementById("verification-error"),
    verificationStatus: document.getElementById("verification-status"),
    postcode: document.getElementById("delivery-postcode"),
    code: document.getElementById("verification-code"),
    verify: document.getElementById("verify-order"),
    message: document.getElementById("message"),
    send: document.getElementById("send"),
    retry: document.getElementById("retry"),
    confirm: document.getElementById("confirm"),
    newIntake: document.getElementById("new-intake"),
    startRecording: document.getElementById("start-recording"),
    stopRecording: document.getElementById("stop-recording"),
    wav: document.getElementById("wav-recording"),
    wavAttach: document.getElementById("wav-attach"),
    audio: document.getElementById("reply-audio"),
    help: document.getElementById("transcribe-help"),
    chatPanel: document.getElementById("support-chat"),
  };

  const state = {
    sessionId: null,
    revision: 0,
    status: "collecting",
    language: "en",
    summary: null,
    pendingReviewRevision: null,
    pendingClarifications: {},
    messages: [],
    pendingTurnMessage: null,
    turnPending: false,
    inputMode: "text",
    generation: 0,
    playbackToken: 0,
    objectUrl: null,
    pending: false,
    sttPending: false,
    createPending: false,
    resumePending: false,
    resumeFailed: false,
    retryKind: null, // "resume" | "turn" | "confirmation" | "create" | null
    retryPayload: null,
    charLimitAlert: false,
    audioBlob: null,
    recording: null,
    recordingPending: false,
    speechPending: null,
    orderVerificationRequired: false,
    verificationPending: false,
    slotOptions: [],
  };

  const transcriptionGate = new window.OrbioRecording.AsyncGate();

  const sttDisabledControls = new WeakMap();

  const encodeWav = window.OrbioRecording.encodeWav;

  function isCanonicalUuid(value) {
    return (
      typeof value === "string" &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
        value
      )
    );
  }

  function newRequestId() {
    return crypto.randomUUID();
  }

  function setStoredId(id) {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  }

  function readStoredId() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null || raw === undefined) return null;
    if (!isCanonicalUuid(raw)) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return raw;
  }

  function textNode(value) {
    return document.createTextNode(value == null ? "" : String(value));
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function showAlert(message) {
    clearChildren(els.alert);
    els.alert.appendChild(textNode(message));
    els.alert.hidden = false;
  }

  function clearAlert() {
    state.charLimitAlert = false;
    clearChildren(els.alert);
    els.alert.hidden = true;
  }

  function setVerificationError(message) {
    clearChildren(els.verificationError);
    els.verificationError.appendChild(textNode(message));
    els.verificationError.hidden = false;
  }

  function clearVerificationError() {
    clearChildren(els.verificationError);
    els.verificationError.hidden = true;
  }

  function clearVerificationStatus() {
    clearChildren(els.verificationStatus);
    els.verificationStatus.hidden = true;
  }

  function resetVerificationForm() {
    els.postcode.value = "";
    els.code.value = "";
    clearVerificationError();
    clearVerificationStatus();
  }

  function renderVerification() {
    els.verification.hidden = !state.orderVerificationRequired;
    els.verify.disabled =
      state.verificationPending ||
      !els.postcode.value.trim() ||
      !els.code.value.trim();
  }

  function setRetry(kind, payload) {
    state.retryKind = kind;
    state.retryPayload = payload || null;
    els.retry.hidden = !kind || kind === "turn";
  }

  function clearRetry() {
    setRetry(null, null);
  }

  function revokeAudioUrl() {
    if (state.objectUrl) {
      try {
        els.audio.pause();
      } catch (_) {
        /* ignore */
      }
      URL.revokeObjectURL(state.objectUrl);
      state.objectUrl = null;
    }
    els.audio.removeAttribute("src");
    els.audio.load();
  }

  function bumpPlayback() {
    state.playbackToken += 1;
    return state.playbackToken;
  }

  function issuesPresent() {
    return Object.keys(state.pendingClarifications || {}).length > 0;
  }

  function canConfirm() {
    return (
      state.status === "awaiting_confirmation" &&
      state.pendingReviewRevision != null &&
      !issuesPresent() &&
      state.status !== "completed"
    );
  }

  function draftValue() {
    return els.message.value;
  }

  function draftNonempty() {
    return draftValue().length > 0;
  }

  const fetchWithDeadline = window.NorthstarRequest.fetchWithDeadline;

  async function parseError(response) {
    try {
      const data = await response.json();
      const code = data && data.error && data.error.code;
      const message = code
        ? i18n.translateError(code, state.language)
        : ui("errors.provider_unavailable");
      const retryable = !!(data && data.error && data.error.retryable);
      const currentRevision =
        data && data.error && data.error.current_revision;
      return { message, code, retryable, currentRevision, data };
    } catch (_) {
      return {
        message: ui("errors.provider_unavailable"),
        code: null,
        retryable: true,
        currentRevision: undefined,
        data: null,
      };
    }
  }

  function transportMessage(error) {
    if (i18n.isLocaleError(error)) return ui("errors.locale_unavailable");
    return error && error.name === "AbortError"
      ? ui("errors.provider_timeout")
      : ui("errors.provider_unavailable");
  }

  Object.assign(app, {
    STORAGE_KEY,
    MAX_CHARS,
    MAX_WAV_BYTES,
    MAX_RECORD_SECONDS,
    DEADLINE_SESSION_MS,
    DEADLINE_TURN_MS,
    DEADLINE_STT_MS,
    DEADLINE_TTS_MS,
    i18n,
    ui,
    ensureLocale,
    speechLabel,
    els,
    state,
    transcriptionGate,
    sttDisabledControls,
    encodeWav,
    isCanonicalUuid,
    newRequestId,
    setStoredId,
    readStoredId,
    textNode,
    clearChildren,
    showAlert,
    clearAlert,
    setVerificationError,
    clearVerificationError,
    clearVerificationStatus,
    resetVerificationForm,
    renderVerification,
    setRetry,
    clearRetry,
    revokeAudioUrl,
    bumpPlayback,
    issuesPresent,
    canConfirm,
    draftValue,
    draftNonempty,
    fetchWithDeadline,
    parseError,
    transportMessage,
  });
})();
