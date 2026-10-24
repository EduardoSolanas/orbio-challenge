(() => {
  "use strict";

  const app = window.NorthstarApp;
  const {
    DEADLINE_SESSION_MS,
    i18n,
    ui,
    els,
    state,
    setStoredId,
    readStoredId,
    showAlert,
    clearAlert,
    resetVerificationForm,
    setRetry,
    clearRetry,
    revokeAudioUrl,
    bumpPlayback,
    fetchWithDeadline,
    parseError,
    transportMessage,
  } = app;
  async function createSession({ generation } = {}) {
    const gen = generation == null ? state.generation : generation;
    const response = await fetchWithDeadline(
      "/api/sessions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: "{}",
      },
      DEADLINE_SESSION_MS
    );
    if (gen !== state.generation) return { stale: true, response };
    if (!response.ok) {
      const err = await parseError(response);
      return { ok: false, err, response };
    }
    const view = await response.json();
    if (gen !== state.generation) return { stale: true, response };
    return { ok: true, view, response };
  }

  async function getSession(sessionId, { generation } = {}) {
    const gen = generation == null ? state.generation : generation;
    const response = await fetchWithDeadline(
      `/api/sessions/${sessionId}`,
      { method: "GET", headers: { Accept: "application/json" } },
      DEADLINE_SESSION_MS
    );
    if (gen !== state.generation) return { stale: true, response };
    return { response };
  }

  async function bootstrap() {
    clearAlert();
    clearRetry();
    const stored = readStoredId();
    if (!stored) {
      state.createPending = true;
      app.updateControls();
      try {
        const result = await createSession();
        if (result.stale) return;
        if (!result.ok) {
          showAlert(result.err.message);
          setRetry("create", null);
          state.createPending = false;
          app.updateControls();
          return;
        }
        await app.applySessionView(result.view);
        clearAlert();
        clearRetry();
      } catch (e) {
        showAlert(transportMessage(e));
        setRetry("create", null);
      } finally {
        state.createPending = false;
        app.updateControls();
      }
      return;
    }

    state.resumePending = true;
    state.resumeFailed = false;
    app.updateControls();
    try {
      const { response, stale } = await getSession(stored);
      if (stale) return;
      if (response.status === 404) {
        state.resumePending = false;
        setStoredId(null);
        state.sessionId = null;
        await bootstrapCreateOnly();
        return;
      }
      if (!response.ok) {
        const err = await parseError(response);
        state.resumePending = false;
        state.resumeFailed = true;
        showAlert(err.message);
        setRetry("resume", { sessionId: stored });
        state.sessionId = stored;
        app.updateControls();
        return;
      }
      const view = await response.json();
      state.resumePending = false;
      state.resumeFailed = false;
      await app.applySessionView(view);
      clearAlert();
      clearRetry();
    } catch (e) {
      state.resumePending = false;
      state.resumeFailed = true;
      if (i18n.isLocaleError(e)) {
        showAlert(ui("errors.locale_unavailable"));
        setRetry("resume", { sessionId: stored });
        app.updateControls();
        return;
      }
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? ui("errors.provider_timeout") : ui("errors.provider_unavailable"));
      setRetry("resume", { sessionId: stored });
      state.sessionId = stored;
      app.updateControls();
    }
  }

  async function bootstrapCreateOnly() {
    state.createPending = true;
    app.updateControls();
    try {
      const result = await createSession();
      if (result.stale) return;
      if (!result.ok) {
        showAlert(result.err.message);
        setRetry("create", null);
        return;
      }
      await app.applySessionView(result.view);
      clearAlert();
      clearRetry();
    } catch (e) {
      showAlert(transportMessage(e));
      setRetry("create", null);
    } finally {
      state.createPending = false;
      app.updateControls();
    }
  }

  async function onRetry() {
    if (!state.retryKind) return;
    if (state.retryKind === "resume") {
      const sid = (state.retryPayload && state.retryPayload.sessionId) || readStoredId();
      if (!sid) return;
      clearAlert();
      state.resumePending = true;
      state.resumeFailed = false;
      app.updateControls();
      try {
        const { response, stale } = await getSession(sid);
        if (stale) return;
        if (!response.ok) {
          const err = await parseError(response);
          state.resumePending = false;
          state.resumeFailed = true;
          showAlert(err.message);
          setRetry("resume", { sessionId: sid });
          return;
        }
        const view = await response.json();
        state.resumePending = false;
        state.resumeFailed = false;
        await app.applySessionView(view);
        clearAlert();
        clearRetry();
      } catch (e) {
        state.resumePending = false;
        state.resumeFailed = true;
        if (i18n.isLocaleError(e)) {
          showAlert(ui("errors.locale_unavailable"));
          setRetry("resume", { sessionId: sid });
          app.updateControls();
          return;
        }
        const aborted = e && e.name === "AbortError";
        showAlert(aborted ? ui("errors.provider_timeout") : ui("errors.provider_unavailable"));
        setRetry("resume", { sessionId: sid });
        app.updateControls();
      }
      return;
    }
    if (state.retryKind === "create") {
      await startNewIntake({ retryOnly: true });
      return;
    }
    if (state.retryKind === "turn" && state.retryPayload) {
      await app.submitTurnPayload(state.retryPayload, { isRetry: true });
      return;
    }
    if (state.retryKind === "confirmation" && state.retryPayload) {
      await app.submitConfirmationCommand(state.retryPayload, { isRetry: true });
    }
  }

  async function startNewIntake({ retryOnly = false } = {}) {
    // Invalidate pending microphone permission before awaiting any new session work.
    state.generation += 1;
    app.discardRecording();
    const gen = state.generation;
    bumpPlayback();
    state.speechPending = null;
    revokeAudioUrl();
    if (state.sttAbort) {
      try {
        state.sttAbort.abort();
      } catch (_) {
        /* ignore */
      }
      state.sttAbort = null;
    }
    state.sttPending = false;
    state.audioBlob = null;
    state.inputMode = "text";
    state.orderVerificationRequired = false;
    state.verificationPending = false;
    resetVerificationForm();
    els.message.value = "";
    if (els.wav) els.wav.value = "";
    clearAlert();
    clearRetry();

    // Keep prior transcript visible read-only until new session publishes
    state.createPending = true;
    state.pending = false;
    app.updateControls();

    try {
      const result = await createSession({ generation: gen });
      if (gen !== state.generation || result.stale) return;
      if (!result.ok) {
        showAlert(result.err.message);
        setRetry("create", null);
        // retain prior id/transcript
        app.updateControls();
        return;
      }
      await app.applySessionView(result.view, { generation: gen });
      clearAlert();
      clearRetry();
    } catch (e) {
      if (gen !== state.generation) return;
      showAlert(transportMessage(e));
      setRetry("create", null);
    } finally {
      if (gen === state.generation) {
        state.createPending = false;
        app.updateControls();
      }
    }
  }

  Object.assign(app, {
    createSession,
    getSession,
    bootstrap,
    bootstrapCreateOnly,
    onRetry,
    startNewIntake,
  });
})();
