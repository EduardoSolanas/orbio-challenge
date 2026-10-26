import { MAX_CHARS } from "./config.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const SPEAKER_PATH =
  "M3 9v6h4l5 4V5L7 9H3Zm13.5 3a4.5 4.5 0 0 0-2.1-3.8v7.6a4.5 4.5 0 0 0 2.1-3.8Zm0-8.3v2.1a8 8 0 0 1 0 12.4v2.1a10 10 0 0 0 0-16.6Z";
/** The quick options, in the order shown. */
const STARTERS = Object.freeze(["report", "question", "installation", "booking"]);
const CLOCK_PATH =
  "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 5v5.59l3.7 3.7-1.4 1.42L11 13.41V7h2Z";

/** Draws the chat page from a ChatState. It never talks to the network. */
export class ChatView {
  constructor({ dom, state, i18n }) {
    this.dom = dom;
    this.state = state;
    this.i18n = i18n;
    this.document = dom.document;
    this.handlers = { playReply: () => {}, bookSlot: () => {}, starter: () => {} };
    /** Controls that were enabled before transcription locked the panel. */
    this.lockedControls = new WeakMap();
  }

  /** Attach the callbacks the rendered buttons call. */
  bind(handlers) {
    Object.assign(this.handlers, handlers);
  }

  ui(key, values) {
    return this.i18n.t(this.state.language, key, values);
  }

  speechLabel(playable, pending, ordinal) {
    if (!playable) return this.ui("speech.unavailable");
    if (pending) return this.ui("speech.loading");
    return this.ui("speech.play", { ordinal });
  }

  setText(node, value) {
    node.textContent = value == null ? "" : String(value);
  }

  draftValue() {
    return this.dom.message.value;
  }

  draftNonempty() {
    return this.draftValue().length > 0;
  }

  // ---- alert and verification form -------------------------------------------

  showAlert(message) {
    this.setText(this.dom.alert, message);
    this.dom.alert.hidden = false;
  }

  clearAlert() {
    this.state.charLimitAlert = false;
    this.setText(this.dom.alert, "");
    this.dom.alert.hidden = true;
  }

  setVerificationError(message) {
    this.setText(this.dom.verificationError, message);
    this.dom.verificationError.hidden = false;
  }

  clearVerificationError() {
    this.setText(this.dom.verificationError, "");
    this.dom.verificationError.hidden = true;
  }

  showVerificationStatus(message) {
    this.setText(this.dom.verificationStatus, message);
    this.dom.verificationStatus.hidden = false;
  }

  clearVerificationStatus() {
    this.setText(this.dom.verificationStatus, "");
    this.dom.verificationStatus.hidden = true;
  }

  resetVerificationForm() {
    this.dom.postcode.value = "";
    this.dom.code.value = "";
    this.clearVerificationError();
    this.clearVerificationStatus();
  }

  renderVerification() {
    const { dom, state } = this;
    dom.verification.hidden = !state.orderVerificationRequired;
    dom.verify.disabled =
      state.verificationPending || !dom.postcode.value.trim() || !dom.code.value.trim();
  }

  // ---- composer and action buttons ---------------------------------------------

  updateControls() {
    const { dom, state } = this;
    if (!state.sttPending) this.unlockPanel();

    const closed = state.isClosed();
    const busy = state.isBusy();
    const verificationBlocked = state.orderVerificationRequired;
    const draftPresent = this.draftNonempty();

    dom.message.readOnly = closed || busy;

    const overLimit = this.draftValue().length > MAX_CHARS;
    const sendOk =
      !!state.sessionId && !closed && !busy && !overLimit && this.draftValue().trim().length > 0;
    dom.send.disabled = !sendOk;

    const confirmable = state.canConfirm();
    dom.confirm.hidden = !confirmable || closed || verificationBlocked;
    dom.confirm.disabled = !confirmable || busy || closed || verificationBlocked;

    dom.help.hidden = !state.sttPending;
    dom.help.textContent = this.ui("ui.transcribing");

    dom.newIntake.disabled =
      state.sttPending ||
      state.pending ||
      state.createPending ||
      state.resumePending ||
      state.resumeFailed ||
      state.verificationPending;

    dom.startRecording.disabled =
      !!state.recording ||
      state.recordingPending ||
      draftPresent ||
      busy ||
      closed ||
      verificationBlocked;
    dom.stopRecording.hidden = !state.recording;
    dom.stopRecording.disabled = !state.recording || state.sttPending;
    dom.wav.disabled = draftPresent || busy || closed || verificationBlocked;
    dom.wavAttach.disabled = dom.wav.disabled;
    dom.retry.disabled = state.sttPending;

    if (overLimit && !state.charLimitAlert) {
      state.charLimitAlert = true;
      this.showAlert(this.ui("ui.char_limit"));
    } else if (!overLimit && state.charLimitAlert) {
      this.clearAlert();
    }

    dom.slotPicker.querySelectorAll("button").forEach((button) => {
      button.disabled = busy || closed;
    });
    dom.starters.querySelectorAll("button").forEach((button) => {
      button.disabled = busy;
    });
    this.renderVerification();

    if (state.sttPending) {
      this.lockPanel();
    } else {
      dom.chatPanel.removeAttribute("aria-busy");
      this.updateSpeechButtons();
    }
  }

  /** Put back every control that transcription had disabled. */
  unlockPanel() {
    this.dom.chatPanel.querySelectorAll("button, input, textarea").forEach((control) => {
      if (!this.lockedControls.has(control)) return;
      control.disabled = this.lockedControls.get(control);
      this.lockedControls.delete(control);
    });
  }

  /** Disable the whole panel while a recording is being transcribed. */
  lockPanel() {
    this.dom.chatPanel.setAttribute("aria-busy", "true");
    this.dom.chatPanel.querySelectorAll("button, input, textarea").forEach((control) => {
      if (!this.lockedControls.has(control)) this.lockedControls.set(control, control.disabled);
      control.disabled = true;
    });
  }

  // ---- review and time buttons ---------------------------------------------------

  renderReview() {
    const { dom, state } = this;
    if (state.canConfirm() && state.summary) {
      this.setText(dom.review, state.summary);
      dom.review.hidden = false;
    } else {
      this.setText(dom.review, "");
      dom.review.hidden = true;
    }
  }

  slotLabel(slot) {
    const start = new Date(slot.starts_at);
    const end = new Date(slot.ends_at);
    const day = new Intl.DateTimeFormat(this.state.language, {
      weekday: "short",
      day: "numeric",
      month: "short",
    }).format(start);
    const clock = new Intl.DateTimeFormat(this.state.language, {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    return `${day}, ${clock.format(start)}–${clock.format(end)}`;
  }

  renderSlots() {
    const picker = this.dom.slotPicker;
    this.setText(picker, "");
    for (const slot of this.state.slotOptions) {
      const button = this.document.createElement("button");
      button.type = "button";
      button.className = "btn btn--pill btn--large skin-outline grow";
      button.dataset.startsAt = slot.starts_at;
      button.textContent = this.slotLabel(slot);
      button.addEventListener("click", () => this.handlers.bookSlot(slot));
      picker.appendChild(button);
    }
    picker.hidden = this.state.slotOptions.length === 0;
  }

  // ---- starter buttons -------------------------------------------------------------

  /** True when the last assistant message is one of the replies that lists what it can do. */
  offersOptions(message) {
    return [this.ui("capability_reply"), this.ui("capability")].includes(message.text);
  }

  /** Starter buttons show on a fresh conversation, under the "what I can do" replies and after a saved request. */
  startersVisible() {
    const { state } = this;
    if (!state.sessionId || state.status === "handed_off" || state.turnPending || state.pendingTurnMessage) {
      return false;
    }
    // After a saved request the receipt asks if there is anything else: offer the options again.
    if (state.status === "completed") return true;
    if (state.slotOptions.length || state.orderVerificationRequired || state.canConfirm()) {
      return false;
    }
    const last = [...state.messages].reverse().find((message) => message.role === "assistant");
    if (!last) return false;
    return !state.messages.some((message) => message.role === "user") || this.offersOptions(last);
  }

  renderStarters() {
    const picker = this.dom.starters;
    this.setText(picker, "");
    const visible = this.startersVisible();
    if (visible) {
      for (const key of STARTERS) {
        const button = this.document.createElement("button");
        button.type = "button";
        button.className = "btn btn--pill btn--large skin-outline grow";
        button.dataset.starter = key;
        button.textContent = this.ui(`starters.${key}`);
        button.addEventListener("click", () => this.handlers.starter(key));
        picker.appendChild(button);
      }
    }
    picker.hidden = !visible;
  }

  // ---- transcript ------------------------------------------------------------------

  createElement(tag, className) {
    const element = this.document.createElement(tag);
    if (className) element.className = className;
    return element;
  }

  speakerIcon(loading = false) {
    const svg = this.document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "icon icon--s");
    const path = this.document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", loading ? CLOCK_PATH : SPEAKER_PATH);
    svg.appendChild(path);
    return svg;
  }

  speechIsPending(messageId) {
    const pending = this.state.speechPending;
    return (
      !!pending && pending.generation === this.state.generation && pending.messageId === messageId
    );
  }

  renderSources(container, citations) {
    if (!citations || !citations.length) return;
    const region = this.createElement("div", "disclosure skin-source");
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", this.ui("ui.sources"));
    for (const cite of citations) {
      const details = this.createElement("details");
      const summary = this.createElement("summary");
      summary.textContent = `${cite.title} ${cite.source_id}`;
      details.appendChild(summary);
      const body = this.createElement("div");
      body.textContent = cite.excerpt;
      details.appendChild(body);
      const version = this.createElement("p", "caption text-muted");
      version.textContent = this.ui("ui.knowledge_base_version", { version: cite.kb_version });
      details.appendChild(version);
      region.appendChild(details);
    }
    container.appendChild(region);
  }

  renderPlayButton(message, playbackNumber) {
    const holder = this.createElement("div", "inline-action");
    const button = this.createElement("button", "btn btn--mini skin-tint");
    button.type = "button";
    button.dataset.messageId = message.id;
    const playable = message.speech_available !== false;
    const pending = playable && this.speechIsPending(message.id);
    button.dataset.playable = playable ? "true" : "false";
    button.dataset.ordinal = String(playbackNumber);
    const label = this.speechLabel(playable, pending, playbackNumber);
    button.setAttribute("aria-label", label);
    button.title = label;
    button.setAttribute("aria-busy", pending ? "true" : "false");
    button.disabled = !playable || pending;
    button.appendChild(this.speakerIcon(pending));
    if (playable) button.addEventListener("click", () => this.handlers.playReply(message.id));
    holder.appendChild(button);
    return holder;
  }

  renderMessage(message, playbackNumber) {
    const isUser = message.role === "user";
    const bubble = this.createElement(
      "article",
      isUser ? "bubble bubble--end skin-forest" : "bubble bubble--start skin-soft"
    );
    const role = this.createElement("span", isUser ? "label label--on-dark" : "label label--muted");
    role.textContent = isUser ? this.ui("ui.you") : this.ui("ui.assistant");
    bubble.appendChild(role);
    const text = this.createElement("div", "wrap-text");
    text.textContent = message.text == null ? "" : String(message.text);
    bubble.appendChild(text);
    if (message.role === "assistant" && message.id) {
      bubble.appendChild(this.renderPlayButton(message, playbackNumber));
      this.renderSources(bubble, message.citations || []);
    }
    return bubble;
  }

  renderThinking() {
    const indicator = this.createElement("div", "bubble bubble--start bubble--fit skin-soft");
    indicator.setAttribute("role", "status");
    indicator.setAttribute("aria-label", this.ui("ui.thinking"));
    const label = this.createElement("span", "u-sr-only");
    label.textContent = this.ui("ui.thinking");
    indicator.appendChild(label);
    const dots = this.createElement("span", "dots text-muted");
    dots.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 3; i += 1) dots.appendChild(this.createElement("span", "dots__dot"));
    indicator.appendChild(dots);
    return indicator;
  }

  renderTranscript() {
    const { dom, state } = this;
    this.setText(dom.log, "");
    let playbackNumber = 0;
    for (const message of state.messages) {
      if (message.role === "assistant" && message.id) playbackNumber += 1;
      dom.log.appendChild(this.renderMessage(message, playbackNumber));
    }
    if (state.pendingTurnMessage) {
      dom.log.appendChild(this.renderMessage(state.pendingTurnMessage, playbackNumber));
    }
    if (state.turnPending) dom.log.appendChild(this.renderThinking());
    dom.log.scrollTop = dom.log.scrollHeight;
  }

  /** Refresh every play button's label and busy state without redrawing the transcript. */
  updateSpeechButtons() {
    for (const button of this.dom.log.querySelectorAll(".btn--mini[data-message-id]")) {
      if (button.dataset.playable !== "true") continue;
      const pending = this.speechIsPending(button.dataset.messageId);
      button.disabled = pending;
      button.setAttribute("aria-busy", pending ? "true" : "false");
      const label = this.speechLabel(true, pending, Number(button.dataset.ordinal));
      button.setAttribute("aria-label", label);
      button.title = label;
      this.setText(button, "");
      button.appendChild(this.speakerIcon(pending));
    }
  }
}
