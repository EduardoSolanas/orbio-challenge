import { Controller } from "./controller.js";

/** Creating, resuming and restarting the conversation. */
export class SessionController extends Controller {
  constructor({ store, ...rest }) {
    super(rest);
    this.store = store;
    this.turns = null;
    this.voice = null;
    this.player = null;
  }

  /** Controllers that depend on each other are wired after construction. */
  attach({ turns, voice, player }) {
    this.turns = turns;
    this.voice = voice;
    this.player = player;
  }

  async createSession({ generation } = {}) {
    const { state, api } = this;
    const gen = generation == null ? state.generation : generation;
    const response = await api.createSession();
    if (gen !== state.generation) return { stale: true, response };
    if (!response.ok) {
      const err = await api.parseError(response);
      return { ok: false, err, response };
    }
    const view = await response.json();
    if (gen !== state.generation) return { stale: true, response };
    return { ok: true, view, response };
  }

  async getSession(sessionId, { generation } = {}) {
    const { state, api } = this;
    const gen = generation == null ? state.generation : generation;
    const response = await api.getSession(sessionId);
    if (gen !== state.generation) return { stale: true, response };
    return { response };
  }

  async bootstrap() {
    const { state, view, presenter } = this;
    view.clearAlert();
    presenter.clearRetry();
    const stored = this.store.load();
    if (!stored) {
      state.createPending = true;
      view.updateControls();
      try {
        const result = await this.createSession();
        if (result.stale) return;
        if (!result.ok) {
          view.showAlert(result.err.message);
          presenter.setRetry("create", null);
          state.createPending = false;
          view.updateControls();
          return;
        }
        await presenter.applySessionView(result.view);
        view.clearAlert();
        presenter.clearRetry();
      } catch (error) {
        this.alertTransportError(error);
        presenter.setRetry("create", null);
      } finally {
        state.createPending = false;
        view.updateControls();
      }
      return;
    }
    await this.resume(stored, { createIfMissing: true });
  }

  /** Reload a stored session; with ``createIfMissing`` an unknown id starts a new one. */
  async resume(sessionId, { createIfMissing = false } = {}) {
    const { state, view, presenter } = this;
    if (!createIfMissing) view.clearAlert();
    state.resumePending = true;
    state.resumeFailed = false;
    view.updateControls();
    try {
      const { response, stale } = await this.getSession(sessionId);
      if (stale) return;
      if (createIfMissing && response.status === 404) {
        state.resumePending = false;
        this.store.save(null);
        state.sessionId = null;
        await this.bootstrapCreateOnly();
        return;
      }
      if (!response.ok) {
        const err = await this.api.parseError(response);
        state.resumePending = false;
        state.resumeFailed = true;
        view.showAlert(err.message);
        presenter.setRetry("resume", { sessionId });
        if (createIfMissing) state.sessionId = sessionId;
        view.updateControls();
        return;
      }
      const sessionView = await response.json();
      state.resumePending = false;
      state.resumeFailed = false;
      await presenter.applySessionView(sessionView);
      view.clearAlert();
      presenter.clearRetry();
    } catch (error) {
      state.resumePending = false;
      state.resumeFailed = true;
      this.alertTransportError(error);
      presenter.setRetry("resume", { sessionId });
      if (!this.i18n.isLocaleError(error)) state.sessionId = sessionId;
      view.updateControls();
    }
  }

  async bootstrapCreateOnly() {
    const { state, view, presenter } = this;
    state.createPending = true;
    view.updateControls();
    try {
      const result = await this.createSession();
      if (result.stale) return;
      if (!result.ok) {
        view.showAlert(result.err.message);
        presenter.setRetry("create", null);
        return;
      }
      await presenter.applySessionView(result.view);
      view.clearAlert();
      presenter.clearRetry();
    } catch (error) {
      this.alertTransportError(error);
      presenter.setRetry("create", null);
    } finally {
      state.createPending = false;
      view.updateControls();
    }
  }

  /** The Retry button: repeat whatever failed last. */
  async onRetry() {
    const { state } = this;
    if (!state.retryKind) return;
    if (state.retryKind === "resume") {
      const sessionId = (state.retryPayload && state.retryPayload.sessionId) || this.store.load();
      if (!sessionId) return;
      await this.resume(sessionId);
      return;
    }
    if (state.retryKind === "create") {
      await this.startNewIntake();
      return;
    }
    if (state.retryKind === "turn" && state.retryPayload) {
      await this.turns.submitTurnPayload(state.retryPayload);
      return;
    }
    if (state.retryKind === "confirmation" && state.retryPayload) {
      await this.turns.submitConfirmationCommand(state.retryPayload);
    }
  }

  async startNewIntake() {
    const { state, view, presenter } = this;
    // Invalidate pending microphone permission before awaiting any new session work.
    state.generation += 1;
    this.voice.discardRecording();
    const gen = state.generation;
    state.bumpPlayback();
    state.speechPending = null;
    this.player.release();
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
    state.slotOptions = [];
    view.renderSlots();
    view.resetVerificationForm();
    view.dom.message.value = "";
    if (view.dom.wav) view.dom.wav.value = "";
    view.clearAlert();
    presenter.clearRetry();

    // Keep the previous transcript visible and read-only until the new session arrives.
    state.createPending = true;
    state.pending = false;
    view.updateControls();

    try {
      const result = await this.createSession({ generation: gen });
      if (gen !== state.generation || result.stale) return;
      if (!result.ok) {
        view.showAlert(result.err.message);
        presenter.setRetry("create", null);
        view.updateControls();
        return;
      }
      await presenter.applySessionView(result.view, { generation: gen });
      view.clearAlert();
      presenter.clearRetry();
    } catch (error) {
      if (gen !== state.generation) return;
      this.alertTransportError(error);
      presenter.setRetry("create", null);
    } finally {
      if (gen === state.generation) {
        state.createPending = false;
        view.updateControls();
      }
    }
  }
}
