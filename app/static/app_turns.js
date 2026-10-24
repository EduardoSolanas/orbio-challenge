(() => {
  "use strict";

  const app = window.NorthstarApp;
  const {
    MAX_CHARS,
    DEADLINE_TURN_MS,
    i18n,
    ui,
    els,
    state,
    newRequestId,
    showAlert,
    clearAlert,
    setVerificationError,
    clearVerificationError,
    clearVerificationStatus,
    setRetry,
    clearRetry,
    canConfirm,
    draftValue,
    fetchWithDeadline,
    parseError,
    transportMessage,
  } = app;
  async function submitTurnPayload(
    payload,
    { isRetry = false, alreadyPending = false } = {}
  ) {
    if (!state.sessionId) return;
    if (!alreadyPending) {
      if (state.pending) return;
      state.pending = true;
    }
    state.pendingTurnMessage = {
      role: "user",
      text: payload.text,
      input_mode: payload.input_mode,
      citations: [],
    };
    state.turnPending = true;
    if (draftValue() === payload.text) {
      els.message.value = "";
      state.inputMode = "text";
    }
    app.renderTranscript();
    app.updateControls();
    const gen = state.generation;
    clearAlert();

    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/turns`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(payload),
        },
        DEADLINE_TURN_MS
      );
      if (gen !== state.generation) return;

      if (response.status === 409) {
        const err = await parseError(response);
        if (err.code === "revision_conflict") {
          // Refresh state; retain unsent text; new request_id required next time
          els.message.value = payload.text;
          state.inputMode = payload.input_mode;
          try {
            const refreshed = await app.getSession(state.sessionId, {
              generation: gen,
            });
            if (gen !== state.generation || refreshed.stale) return;
            if (refreshed.response.ok) {
              const view = await refreshed.response.json();
              if (gen !== state.generation) return;
              await app.applySessionView(view, { generation: gen });
            }
          } catch (_) {
            /* keep local */
          }
          showAlert(err.message);
          clearRetry();
          state.retryPayload = null;
          return;
        }
        showAlert(err.message);
        if (err.retryable) setRetry("turn", payload);
        else clearRetry();
        els.message.value = payload.text;
        return;
      }

      if (!response.ok) {
        const err = await parseError(response);
        showAlert(err.message);
        els.message.value = payload.text;
        state.inputMode = payload.input_mode;
        if (err.retryable || response.status >= 500) {
          setRetry("turn", payload);
        } else {
          clearRetry();
        }
        return;
      }

      const data = await response.json();
      if (gen !== state.generation) return;
      state.pendingTurnMessage = null;
      state.turnPending = false;
      await app.applyTurnResponse(data, payload.text, payload.input_mode, gen);
      els.message.value = "";
      state.inputMode = "text";
      state.audioBlob = null;
      if (els.wav) els.wav.value = "";
      clearAlert();
      clearRetry();
    } catch (e) {
      if (gen !== state.generation) return;
      if (i18n.isLocaleError(e)) {
        showAlert(ui("errors.locale_unavailable"));
        els.message.value = payload.text;
        state.inputMode = payload.input_mode;
        setRetry("turn", payload);
        return;
      }
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? ui("errors.provider_timeout") : ui("errors.provider_unavailable"));
      els.message.value = payload.text;
      state.inputMode = payload.input_mode;
      setRetry("turn", payload);
    } finally {
      if (gen === state.generation) {
        state.pendingTurnMessage = null;
        state.turnPending = false;
        state.pending = false;
        app.renderTranscript();
        app.updateControls();
      }
    }
  }

  async function onSend() {
    if (els.send.disabled || state.pending || state.createPending || state.sttPending) {
      return;
    }
    const text = draftValue();
    if (!text.trim() || text.length > MAX_CHARS) return;
    clearRetry();
    const payload = {
      request_id: newRequestId(),
      expected_revision: state.revision,
      text,
      input_mode: state.inputMode === "transcript" ? "transcript" : "text",
    };
    state.retryPayload = payload;
    state.pending = true;
    app.updateControls();
    await submitTurnPayload(payload, { alreadyPending: true });
  }

  async function submitConfirmationCommand(command) {
    if (!state.sessionId) return;
    const gen = state.generation;
    state.pending = true;
    app.updateControls();
    clearAlert();
    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/confirm`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: command.body,
        },
        DEADLINE_TURN_MS
      );
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await parseError(response);
        showAlert(err.message);
        if (err.retryable || response.status >= 500) {
          setRetry("confirmation", command);
        } else {
          clearRetry();
        }
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      await app.applyTurnResponse(data, null, null, gen);
      // Re-sync from GET so an idempotent replay after a lost response shows
      // the durable completed state and receipt.
      try {
        const refreshed = await app.getSession(state.sessionId, { generation: gen });
        if (
          gen === state.generation &&
          !refreshed.stale &&
          refreshed.response.ok
        ) {
          const view = await refreshed.response.json();
          if (gen === state.generation) await app.applySessionView(view, { generation: gen });
        }
      } catch (_) {
        /* turn response already applied */
      }
      clearAlert();
      clearRetry();
    } catch (e) {
      if (gen !== state.generation) return;
      showAlert(transportMessage(e));
      setRetry("confirmation", command);
    } finally {
      if (gen === state.generation) {
        state.pending = false;
        app.updateControls();
      }
    }
  }

  async function onConfirm() {
    if (
      els.confirm.disabled ||
      els.confirm.hidden ||
      state.pending ||
      state.createPending
    ) {
      return;
    }
    if (!canConfirm() || !state.sessionId) return;
    const payload = {
      request_id: newRequestId(),
      expected_revision: state.revision,
      review_revision: state.pendingReviewRevision,
    };
    // Keep the exact serialized command: Retry must not construct a new one.
    const command = window.NorthstarConfirmationRetry.createConfirmationCommand(
      payload
    );
    clearRetry();
    await submitConfirmationCommand(command);
  }

  async function onVerifyOrder() {
    if (
      !state.sessionId ||
      !state.orderVerificationRequired ||
      state.verificationPending
    ) {
      return;
    }
    const postcode = els.postcode.value;
    const code = els.code.value;
    if (!postcode.trim() || !code.trim()) return;
    const gen = state.generation;
    state.verificationPending = true;
    clearVerificationError();
    app.updateControls();
    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/order-verification`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify({
            delivery_postcode: postcode,
            verification_code: code,
          }),
        },
        DEADLINE_TURN_MS
      );
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await parseError(response);
        if (gen === state.generation) setVerificationError(err.message);
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      if (data && data.verified === true) {
        state.orderVerificationRequired = false;
        els.postcode.value = "";
        els.code.value = "";
        clearVerificationError();
        els.verificationStatus.textContent = ui("auth.verified");
        els.verificationStatus.hidden = false;
      } else {
        // One localized message for a wrong code, postcode or unknown order.
        setVerificationError(ui("auth.verification_failed"));
        clearVerificationStatus();
      }
    } catch (e) {
      if (gen !== state.generation) return;
      if (i18n.isLocaleError(e)) {
        setVerificationError(ui("errors.locale_unavailable"));
        return;
      }
      const aborted = e && e.name === "AbortError";
      setVerificationError(
        aborted ? ui("errors.provider_timeout") : ui("errors.provider_unavailable")
      );
    } finally {
      if (gen === state.generation) {
        state.verificationPending = false;
        app.updateControls();
      }
    }
  }

  Object.assign(app, {
    submitTurnPayload,
    onSend,
    submitConfirmationCommand,
    onConfirm,
    onVerifyOrder,
  });
})();
