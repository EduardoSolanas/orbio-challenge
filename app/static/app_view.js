(() => {
  "use strict";

  const app = window.NorthstarApp;
  const {
    MAX_CHARS,
    i18n,
    ui,
    ensureLocale,
    speechLabel,
    els,
    state,
    sttDisabledControls,
    setStoredId,
    textNode,
    clearChildren,
    showAlert,
    clearAlert,
    clearVerificationError,
    resetVerificationForm,
    renderVerification,
    canConfirm,
    draftValue,
    draftNonempty,
  } = app;
  function updateControls() {
    if (!state.sttPending) {
      els.chatPanel.querySelectorAll("button, input, textarea").forEach((control) => {
        if (!sttDisabledControls.has(control)) return;
        control.disabled = sttDisabledControls.get(control);
        sttDisabledControls.delete(control);
      });
    }
    const completed = state.status === "completed" || state.status === "handed_off";
    const verificationBlocked = state.orderVerificationRequired;
    const mutating =
      state.pending ||
      state.createPending ||
      state.sttPending ||
      state.recordingPending ||
      !!state.recording ||
      state.resumePending ||
      state.resumeFailed ||
      state.verificationPending;
    const readonly =
      completed ||
      state.pending ||
      state.createPending ||
      state.sttPending ||
      state.recordingPending ||
      !!state.recording ||
      state.resumePending ||
      state.resumeFailed ||
      state.verificationPending;

    els.message.readOnly = readonly;
    if (completed) {
      els.message.setAttribute("readonly", "readonly");
    }

    const overLimit = draftValue().length > MAX_CHARS;
    const sendOk =
      !!state.sessionId &&
      !completed &&
      !mutating &&
      !overLimit &&
      draftValue().trim().length > 0;

    els.send.disabled = !sendOk;
    els.confirm.hidden = !canConfirm() || completed || verificationBlocked;
    els.confirm.disabled =
      !canConfirm() || mutating || completed || verificationBlocked;

    els.help.hidden = !state.sttPending;
    els.help.textContent = ui("ui.transcribing");

    els.newIntake.disabled =
      state.sttPending ||
      state.pending ||
      state.createPending ||
      state.resumePending ||
      state.resumeFailed ||
      state.verificationPending;

    els.startRecording.disabled =
      !!state.recording ||
      state.recordingPending ||
      draftNonempty() ||
      mutating ||
      completed ||
      verificationBlocked;
    els.stopRecording.hidden = !state.recording;
    els.stopRecording.disabled = !state.recording || state.sttPending;
    els.wav.disabled = draftNonempty() || mutating || completed || verificationBlocked;
    els.wavAttach.disabled = els.wav.disabled;
    els.retry.disabled = state.sttPending;

    if (overLimit && !state.charLimitAlert) {
      state.charLimitAlert = true;
      showAlert(ui("ui.char_limit"));
    } else if (!overLimit && state.charLimitAlert) {
      clearAlert();
    }

    els.slotPicker.querySelectorAll("button").forEach((button) => {
      button.disabled = mutating || completed;
    });
    renderVerification();
    if (state.sttPending) {
      els.chatPanel.setAttribute("aria-busy", "true");
      els.chatPanel.querySelectorAll("button, input, textarea").forEach((control) => {
        if (!sttDisabledControls.has(control)) {
          sttDisabledControls.set(control, control.disabled);
        }
        control.disabled = true;
      });
    } else {
      els.chatPanel.removeAttribute("aria-busy");
      updateSpeechButtons();
    }
  }

  function renderReview() {
    if (canConfirm() && state.summary) {
      clearChildren(els.review);
      els.review.appendChild(textNode(state.summary));
      els.review.hidden = false;
    } else {
      clearChildren(els.review);
      els.review.hidden = true;
    }
  }

  function slotLabel(slot) {
    const start = new Date(slot.starts_at);
    const end = new Date(slot.ends_at);
    const day = new Intl.DateTimeFormat(state.language, {
      weekday: "short",
      day: "numeric",
      month: "short",
    }).format(start);
    const clock = new Intl.DateTimeFormat(state.language, {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    return `${day}, ${clock.format(start)}–${clock.format(end)}`;
  }

  function renderSlots() {
    clearChildren(els.slotPicker);
    for (const slot of state.slotOptions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn slot";
      button.dataset.startsAt = slot.starts_at;
      button.appendChild(textNode(slotLabel(slot)));
      button.addEventListener("click", () => app.onBookSlot(slot));
      els.slotPicker.appendChild(button);
    }
    els.slotPicker.hidden = state.slotOptions.length === 0;
  }

  function renderSources(container, citations) {
    if (!citations || !citations.length) return;
    const region = document.createElement("div");
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", ui("ui.sources"));
    region.className = "sources";
    for (const cite of citations) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.appendChild(
        textNode(`${cite.title} ${cite.source_id}`)
      );
      details.appendChild(summary);
      const body = document.createElement("div");
      body.className = "sources__body";
      body.appendChild(textNode(cite.excerpt));
      details.appendChild(body);
      const version = document.createElement("p");
      version.className = "sources__version";
      version.appendChild(
        textNode(ui("ui.knowledge_base_version", { version: cite.kb_version }))
      );
      details.appendChild(version);
      region.appendChild(details);
    }
    container.appendChild(region);
  }

  function appendSpeakerIcon(button, loading = false) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("focusable", "false");
    svg.classList.add(loading ? "speaker-icon--loading" : "speaker-icon");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", loading
      ? "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 5v5.59l3.7 3.7-1.4 1.42L11 13.41V7h2Z"
      : "M3 9v6h4l5 4V5L7 9H3Zm13.5 3a4.5 4.5 0 0 0-2.1-3.8v7.6a4.5 4.5 0 0 0 2.1-3.8Zm0-8.3v2.1a8 8 0 0 1 0 12.4v2.1a10 10 0 0 0 0-16.6Z");
    svg.appendChild(path);
    button.appendChild(svg);
  }

  function speechIsPending(messageId) {
    const pending = state.speechPending;
    return !!pending && pending.generation === state.generation && pending.messageId === messageId;
  }

  function renderMessage(msg, playbackNumber) {
    const wrap = document.createElement("article");
    wrap.className = `msg msg--${msg.role}`;
    const role = document.createElement("span");
    role.className = "msg__role";
    role.appendChild(textNode(msg.role === "user" ? ui("ui.you") : ui("ui.assistant")));
    wrap.appendChild(role);
    const text = document.createElement("div");
    text.className = "msg__text";
    text.appendChild(textNode(msg.text));
    wrap.appendChild(text);
    if (msg.role === "assistant" && msg.id) {
      const playWrap = document.createElement("div");
      playWrap.className = "msg__play";
      const play = document.createElement("button");
      play.type = "button";
      play.className = "btn msg__play-button";
      play.dataset.messageId = msg.id;
      const playable = msg.speech_available !== false;
      const pending = playable && speechIsPending(msg.id);
      play.dataset.playable = playable ? "true" : "false";
      play.dataset.ordinal = String(playbackNumber);
      const label = speechLabel(playable, pending, playbackNumber);
      play.setAttribute("aria-label", label);
      play.title = label;
      play.setAttribute("aria-busy", pending ? "true" : "false");
      play.disabled = !playable || pending;
      appendSpeakerIcon(play, pending);
      if (playable) {
        play.addEventListener("click", () => app.playReply(msg.id));
      }
      playWrap.appendChild(play);
      wrap.appendChild(playWrap);
      renderSources(wrap, msg.citations || []);
    }
    return wrap;
  }

  function updateSpeechButtons() {
    const buttons = els.log.querySelectorAll(".msg__play-button");
    for (const button of buttons) {
      if (button.dataset.playable !== "true") continue;
      const pending = speechIsPending(button.dataset.messageId);
      button.disabled = pending;
      button.setAttribute("aria-busy", pending ? "true" : "false");
      const label = speechLabel(true, pending, Number(button.dataset.ordinal));
      button.setAttribute("aria-label", label);
      button.title = label;
      clearChildren(button);
      appendSpeakerIcon(button, pending);
    }
  }

  function renderTranscript() {
    clearChildren(els.log);
    let playbackNumber = 0;
    for (const msg of state.messages) {
      if (msg.role === "assistant" && msg.id) playbackNumber += 1;
      els.log.appendChild(renderMessage(msg, playbackNumber));
    }
    if (state.pendingTurnMessage) {
      els.log.appendChild(renderMessage(state.pendingTurnMessage, playbackNumber));
    }
    if (state.turnPending) {
      const indicator = document.createElement("div");
      indicator.className = "msg msg--assistant msg--thinking";
      indicator.setAttribute("role", "status");
      indicator.setAttribute("aria-label", ui("ui.thinking"));
      const label = document.createElement("span");
      label.className = "sr-only";
      label.appendChild(textNode(ui("ui.thinking")));
      indicator.appendChild(label);
      const dots = document.createElement("span");
      dots.className = "msg__thinking-dots";
      dots.setAttribute("aria-hidden", "true");
      for (let i = 0; i < 3; i += 1) dots.appendChild(document.createElement("span"));
      indicator.appendChild(dots);
      els.log.appendChild(indicator);
    }
    els.log.scrollTop = els.log.scrollHeight;
  }

  async function applySessionView(view, { publishId = true, generation = null } = {}) {
    await ensureLocale(view.reply_language);
    if (generation != null && generation !== state.generation) return;
    if (publishId) {
      state.sessionId = view.session_id;
      setStoredId(view.session_id);
    }
    state.revision = view.revision;
    state.status = view.status;
    state.language = i18n.languageOf(view.reply_language);
    state.summary = view.summary;
    state.pendingReviewRevision = view.pending_review_revision;
    state.pendingClarifications = view.pending_clarifications || {};
    state.orderVerificationRequired = false;
    resetVerificationForm();
    state.messages = (view.messages || []).map((m) => ({
      id: m.id,
      role: m.role,
      text: m.text,
      input_mode: m.input_mode,
      language: m.language,
      speech_available: m.speech_available,
      citations: m.citations || [],
    }));
    state.slotOptions = [];
    i18n.applyStaticLabels(state.language);
    renderTranscript();
    renderReview();
    renderSlots();
    updateControls();
  }

  async function applyTurnResponse(response, userText, inputMode, generation = null) {
    await ensureLocale(response.reply_language);
    if (generation != null && generation !== state.generation) return;
    state.revision = response.revision;
    state.status = response.status;
    state.language = i18n.languageOf(response.reply_language);
    state.summary = response.summary;
    state.pendingReviewRevision = response.pending_review_revision;
    state.pendingClarifications = response.pending_clarifications || {};
    const verificationWasRequired = state.orderVerificationRequired;
    state.orderVerificationRequired = !!response.order_verification_required;
    if (state.orderVerificationRequired && !verificationWasRequired) {
      resetVerificationForm();
      els.postcode.focus();
    } else if (!state.orderVerificationRequired) {
      clearVerificationError();
    }
    if (userText != null) {
      state.messages.push({
        role: "user",
        text: userText,
        input_mode: inputMode,
        citations: [],
      });
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
    i18n.applyStaticLabels(state.language);
    renderTranscript();
    renderReview();
    renderSlots();
    updateControls();
  }

  Object.assign(app, {
    updateControls,
    renderReview,
    renderSlots,
    renderSources,
    appendSpeakerIcon,
    speechIsPending,
    renderMessage,
    updateSpeechButtons,
    renderTranscript,
    applySessionView,
    applyTurnResponse,
  });
})();
