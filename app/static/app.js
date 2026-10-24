(() => {
  "use strict";

  const app = window.NorthstarApp;
  const {
    i18n,
    els,
    state,
    clearRetry,
    revokeAudioUrl,
    draftNonempty,
  } = app;
  function onMessageInput() {
    // Editing a failed draft cancels retry payload
    if (state.retryKind === "turn") {
      clearRetry();
    }
    // Fresh typing after send uses text mode unless still in transcript draft
    if (state.inputMode === "transcript" && !draftNonempty()) {
      state.inputMode = "text";
    }
    app.updateControls();
  }

  function onPageHide() {
    state.generation += 1;
    state.speechPending = null;
    app.discardRecording();
    try {
      els.audio.pause();
    } catch (_) {
      /* ignore */
    }
    revokeAudioUrl();
  }

  els.send.addEventListener("click", () => {
    app.onSend();
  });

  els.retry.addEventListener("click", () => {
    app.onRetry();
  });

  els.confirm.addEventListener("click", () => {
    app.onConfirm();
  });

  els.newIntake.addEventListener("click", () => {
    app.startNewIntake();
  });

  els.startRecording.addEventListener("click", () => {
    app.startRecording();
  });

  els.stopRecording.addEventListener("click", () => {
    app.stopRecording();
  });

  els.wavAttach.addEventListener("click", () => {
    if (!els.wavAttach.disabled) els.wav.click();
  });

  els.wav.addEventListener("change", app.onWavSelected);

  els.verify.addEventListener("click", () => {
    app.onVerifyOrder();
  });

  els.postcode.addEventListener("input", app.updateControls);

  els.code.addEventListener("input", app.updateControls);

  els.message.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      app.onSend();
    }
  });

  els.message.addEventListener("input", onMessageInput);

  window.addEventListener("pagehide", onPageHide);

  i18n.applyStaticLabels(state.language);

  app.updateControls();

  app.bootstrap();
})();
