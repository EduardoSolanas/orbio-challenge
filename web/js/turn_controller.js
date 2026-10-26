import { MAX_CHARS } from "./config.js";
import { ConfirmationCommand } from "./confirmation.js";
import { Controller } from "./controller.js";

/** Sending messages and the dedicated confirm, verify and booking requests. */
export class TurnController extends Controller {
  constructor({ newRequestId, ...rest }) {
    super(rest);
    this.newRequestId = newRequestId;
    this.session = null;
  }

  attach({ session }) {
    this.session = session;
  }

  /** Send one turn. ``alreadyPending`` is set when the caller has already taken the busy flag. */
  async submitTurnPayload(payload, { alreadyPending = false } = {}) {
    const { state, view, presenter, api } = this;
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
    if (view.draftValue() === payload.text) {
      view.dom.message.value = "";
      state.inputMode = "text";
    }
    view.renderTranscript();
    view.renderStarters();
    view.updateControls();
    const gen = state.generation;
    view.clearAlert();

    try {
      const response = await api.postTurn(state.sessionId, payload);
      if (gen !== state.generation) return;

      if (response.status === 409) {
        await this.handleConflict(response, payload, gen);
        return;
      }
      if (!response.ok) {
        const err = await api.parseError(response);
        view.showAlert(err.message);
        view.dom.message.value = payload.text;
        state.inputMode = payload.input_mode;
        if (err.retryable || response.status >= 500) presenter.setRetry("turn", payload);
        else presenter.clearRetry();
        return;
      }

      const data = await response.json();
      if (gen !== state.generation) return;
      state.pendingTurnMessage = null;
      state.turnPending = false;
      await presenter.applyTurnResponse(data, payload.text, payload.input_mode, gen);
      view.dom.message.value = "";
      state.inputMode = "text";
      state.audioBlob = null;
      if (view.dom.wav) view.dom.wav.value = "";
      view.clearAlert();
      presenter.clearRetry();
    } catch (error) {
      if (gen !== state.generation) return;
      this.alertTransportError(error);
      view.dom.message.value = payload.text;
      state.inputMode = payload.input_mode;
      presenter.setRetry("turn", payload);
    } finally {
      if (gen === state.generation) {
        state.pendingTurnMessage = null;
        state.turnPending = false;
        state.pending = false;
        view.renderTranscript();
        view.renderStarters();
        view.updateControls();
      }
    }
  }

  /** A 409: either the session moved on (refresh it) or a fixed rule refused the turn. */
  async handleConflict(response, payload, gen) {
    const { state, view, presenter, api } = this;
    const err = await api.parseError(response);
    if (err.code === "revision_conflict") {
      // Keep the unsent text; a new request id is needed next time.
      view.dom.message.value = payload.text;
      state.inputMode = payload.input_mode;
      try {
        const refreshed = await this.session.getSession(state.sessionId, { generation: gen });
        if (gen !== state.generation || refreshed.stale) return;
        if (refreshed.response.ok) {
          const sessionView = await refreshed.response.json();
          if (gen !== state.generation) return;
          await presenter.applySessionView(sessionView, { generation: gen });
        }
      } catch (_) {
        /* keep the local state */
      }
      view.showAlert(err.message);
      presenter.clearRetry();
      state.retryPayload = null;
      return;
    }
    view.showAlert(err.message);
    if (err.retryable) presenter.setRetry("turn", payload);
    else presenter.clearRetry();
    view.dom.message.value = payload.text;
  }

  async onSend() {
    const { state, view, presenter } = this;
    if (view.dom.send.disabled || state.pending || state.createPending || state.sttPending) return;
    const text = view.draftValue();
    if (!text.trim() || text.length > MAX_CHARS) return;
    if (state.status === "completed") {
      // The saved request is finished: typing opens a fresh conversation and sends the text there.
      await this.session.startNewIntake();
      if (state.status === "completed") {
        view.dom.message.value = text; // it could not be created; keep what was typed
        view.updateControls();
        return;
      }
    }
    presenter.clearRetry();
    const payload = {
      request_id: this.newRequestId(),
      expected_revision: state.revision,
      text,
      input_mode: state.inputMode === "transcript" ? "transcript" : "text",
    };
    state.retryPayload = payload;
    state.pending = true;
    view.updateControls();
    await this.submitTurnPayload(payload, { alreadyPending: true });
  }

  /** A starter button: most send a ready-made message, the question one just opens the box. */
  async onStarter(key) {
    const { state, view, presenter } = this;
    if (!state.sessionId || state.status === "handed_off") return;
    if (state.pending || state.createPending || state.sttPending) return;
    if (state.status === "completed") {
      // A finished conversation is closed, so the choice opens a fresh one first.
      await this.session.startNewIntake();
      if (state.status === "completed") return; // it could not be created; the alert says why
    }
    if (key === "question") {
      view.dom.message.focus();
      return;
    }
    presenter.clearRetry();
    const payload = {
      request_id: this.newRequestId(),
      expected_revision: state.revision,
      text: view.ui(`starters.${key}_message`),
      input_mode: "text",
    };
    state.retryPayload = payload;
    state.pending = true;
    view.updateControls();
    await this.submitTurnPayload(payload, { alreadyPending: true });
  }

  async submitConfirmationCommand(command) {
    const { state, view, presenter, api } = this;
    if (!state.sessionId) return;
    const gen = state.generation;
    state.pending = true;
    view.updateControls();
    view.clearAlert();
    try {
      const response = await api.postConfirm(state.sessionId, command.body);
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await api.parseError(response);
        view.showAlert(err.message);
        if (err.retryable || response.status >= 500) presenter.setRetry("confirmation", command);
        else presenter.clearRetry();
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      await presenter.applyTurnResponse(data, null, null, gen);
      await this.resync(gen);
      view.clearAlert();
      presenter.clearRetry();
    } catch (error) {
      if (gen !== state.generation) return;
      this.alertTransportError(error);
      presenter.setRetry("confirmation", command);
    } finally {
      if (gen === state.generation) {
        state.pending = false;
        view.updateControls();
      }
    }
  }

  /**
   * Reload the session from the server so the transcript matches what was stored; this also
   * shows the durable result when a replayed request answers a lost response.
   */
  async resync(gen) {
    const { state, presenter } = this;
    try {
      const refreshed = await this.session.getSession(state.sessionId, { generation: gen });
      if (gen === state.generation && !refreshed.stale && refreshed.response.ok) {
        const sessionView = await refreshed.response.json();
        if (gen === state.generation) await presenter.applySessionView(sessionView, { generation: gen });
      }
    } catch (_) {
      /* the turn response is already applied */
    }
  }

  async onConfirm() {
    const { state, view, presenter } = this;
    if (view.dom.confirm.disabled || view.dom.confirm.hidden || state.pending || state.createPending) {
      return;
    }
    if (!state.canConfirm() || !state.sessionId) return;
    const command = ConfirmationCommand.from({
      request_id: this.newRequestId(),
      expected_revision: state.revision,
      review_revision: state.pendingReviewRevision,
    });
    presenter.clearRetry();
    await this.submitConfirmationCommand(command);
  }

  async onBookSlot(slot) {
    const { state, view, api } = this;
    if (!state.sessionId || state.pending || state.createPending) return;
    const gen = state.generation;
    state.pending = true;
    view.updateControls();
    view.clearAlert();
    try {
      const response = await api.postBooking(state.sessionId, {
        request_id: this.newRequestId(),
        expected_revision: state.revision,
        starts_at: slot.starts_at,
      });
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await api.parseError(response);
        view.showAlert(err.message);
        if (err.code === "slot_unavailable") {
          state.slotOptions = [];
          view.renderSlots();
        }
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      await this.presenter.applyTurnResponse(data, null, null, gen);
      await this.resync(gen);
    } catch (error) {
      if (gen !== state.generation) return;
      this.alertTransportError(error);
    } finally {
      if (gen === state.generation) {
        state.pending = false;
        view.updateControls();
      }
    }
  }

  async onVerifyOrder() {
    const { state, view, presenter, api } = this;
    if (!state.sessionId || !state.orderVerificationRequired || state.verificationPending) return;
    const postcode = view.dom.postcode.value;
    const code = view.dom.code.value;
    if (!postcode.trim() || !code.trim()) return;
    const gen = state.generation;
    state.verificationPending = true;
    view.clearVerificationError();
    view.updateControls();
    try {
      const response = await api.postVerification(state.sessionId, {
        delivery_postcode: postcode,
        verification_code: code,
      });
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await api.parseError(response);
        if (gen === state.generation) view.setVerificationError(err.message);
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      if (data && data.verified === true) {
        state.orderVerificationRequired = false;
        view.dom.postcode.value = "";
        view.dom.code.value = "";
        view.clearVerificationError();
        view.showVerificationStatus(this.ui("auth.verified"));
        // The request made before verifying (a booking, an installation check) is answered now.
        if (data.turn) await presenter.applyTurnResponse(data.turn, null, null, gen);
      } else {
        // One localised message for a wrong code, postcode or unknown order.
        view.setVerificationError(this.ui("auth.verification_failed"));
        view.clearVerificationStatus();
      }
    } catch (error) {
      if (gen !== state.generation) return;
      view.setVerificationError(api.transportMessage(error));
    } finally {
      if (gen === state.generation) {
        state.verificationPending = false;
        view.updateControls();
      }
    }
  }
}
