import { I18n } from "./i18n.js";

/** Applies server responses to the state and redraws the page. */
export class ChatPresenter {
  constructor({ state, view, i18n, store }) {
    this.state = state;
    this.view = view;
    this.i18n = i18n;
    this.store = store;
  }

  /** Show the retry button for every failure except a failed turn (which resends from the composer). */
  setRetry(kind, payload) {
    this.state.retryKind = kind;
    this.state.retryPayload = payload || null;
    this.view.dom.retry.hidden = !kind || kind === "turn";
  }

  clearRetry() {
    this.setRetry(null, null);
  }

  /** Load a whole session (create, resume, refresh). Stale generations are ignored. */
  async applySessionView(sessionView, { publishId = true, generation = null } = {}) {
    const { state, view } = this;
    await this.i18n.ensure(sessionView.reply_language);
    if (generation != null && generation !== state.generation) return;
    if (publishId) {
      state.sessionId = sessionView.session_id;
      this.store.save(sessionView.session_id);
    }
    state.revision = sessionView.revision;
    state.status = sessionView.status;
    state.language = I18n.languageOf(sessionView.reply_language);
    state.summary = sessionView.summary;
    state.pendingReviewRevision = sessionView.pending_review_revision;
    state.pendingClarifications = sessionView.pending_clarifications || {};
    state.orderVerificationRequired = false;
    view.resetVerificationForm();
    state.messages = (sessionView.messages || []).map((message) => ({
      id: message.id,
      role: message.role,
      text: message.text,
      input_mode: message.input_mode,
      language: message.language,
      speech_available: message.speech_available,
      citations: message.citations || [],
    }));
    state.slotOptions = [];
    this.redraw();
  }

  /** Append one reply (and the user's message that caused it) to the conversation. */
  async applyTurnResponse(response, userText, inputMode, generation = null) {
    const { state, view } = this;
    await this.i18n.ensure(response.reply_language);
    if (generation != null && generation !== state.generation) return;
    state.revision = response.revision;
    state.status = response.status;
    state.language = I18n.languageOf(response.reply_language);
    state.summary = response.summary;
    state.pendingReviewRevision = response.pending_review_revision;
    state.pendingClarifications = response.pending_clarifications || {};
    const verificationWasRequired = state.orderVerificationRequired;
    state.orderVerificationRequired = !!response.order_verification_required;
    if (state.orderVerificationRequired && !verificationWasRequired) {
      view.resetVerificationForm();
      view.dom.postcode.focus();
    } else if (!state.orderVerificationRequired) {
      view.clearVerificationError();
    }
    if (userText != null) {
      state.messages.push({ role: "user", text: userText, input_mode: inputMode, citations: [] });
    }
    state.messages.push({
      id: response.message_id,
      role: "assistant",
      text: response.reply,
      input_mode: "system",
      language: response.reply_language,
      speech_available: response.speech_available,
      citations: response.citations || [],
    });
    state.slotOptions = response.slot_options || [];
    this.redraw();
  }

  redraw() {
    const { state, view } = this;
    this.i18n.applyStaticLabels(state.language);
    view.renderTranscript();
    view.renderReview();
    view.renderSlots();
    view.updateControls();
  }
}
