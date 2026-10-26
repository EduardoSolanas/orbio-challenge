import { ApiClient } from "./api.js";
import { AudioPlayer } from "./audio_player.js";
import { ChatDom } from "./dom.js";
import { DeadlineHttp } from "./http.js";
import { I18n } from "./i18n.js";
import { ChatPresenter } from "./presenter.js";
import { AsyncGate } from "./recording.js";
import { SessionController } from "./session_controller.js";
import { SessionStore } from "./session_store.js";
import { ChatState } from "./state.js";
import { TurnController } from "./turn_controller.js";
import { ChatView } from "./view.js";
import { VoiceController } from "./voice_controller.js";

/** How long a copy button shows its tick. */
const COPIED_MS = 1500;

/** Builds the page's objects, connects the DOM events to them and starts the conversation. */
export class App {
  /** ``http`` and ``newRequestId`` can be injected; by default they use the browser's. */
  constructor({ window: win, http = null, newRequestId = () => win.crypto.randomUUID() }) {
    this.window = win;
    const document = win.document;

    this.i18n = new I18n({ document, fetchFn: (...args) => win.fetch(...args) });
    this.state = new ChatState();
    this.dom = new ChatDom(document);
    this.store = new SessionStore(win.localStorage);
    this.view = new ChatView({ dom: this.dom, state: this.state, i18n: this.i18n });
    this.presenter = new ChatPresenter({
      state: this.state,
      view: this.view,
      i18n: this.i18n,
      store: this.store,
    });
    const transport =
      http ??
      new DeadlineHttp({
        fetchFn: (...args) => win.fetch(...args),
        ResponseCtor: win.Response,
        AbortControllerCtor: win.AbortController,
      });
    this.api = new ApiClient({
      http: transport,
      i18n: this.i18n,
      getLanguage: () => this.state.language,
    });
    this.player = new AudioPlayer({ element: this.dom.audio, state: this.state, urls: win.URL });

    const shared = {
      state: this.state,
      view: this.view,
      presenter: this.presenter,
      api: this.api,
      i18n: this.i18n,
    };
    this.session = new SessionController({ ...shared, store: this.store });
    this.turns = new TurnController({ ...shared, newRequestId });
    this.voice = new VoiceController({
      ...shared,
      player: this.player,
      gate: new AsyncGate(),
      window: win,
    });
    this.session.attach({ turns: this.turns, voice: this.voice, player: this.player });
    this.turns.attach({ session: this.session });
    this.view.bind({
      playReply: (messageId) => this.voice.playReply(messageId),
      bookSlot: (slot) => this.turns.onBookSlot(slot),
      starter: (key) => this.turns.onStarter(key),
    });
  }

  async start() {
    await this.i18n.init();
    this.i18n.applyStaticLabels(this.state.language);
    this.bindEvents();
    this.view.updateControls();
    return this.session.bootstrap();
  }

  bindEvents() {
    const { dom, window: win } = this;
    dom.send.addEventListener("click", () => this.turns.onSend());
    dom.retry.addEventListener("click", () => this.session.onRetry());
    dom.confirm.addEventListener("click", () => this.turns.onConfirm());
    dom.newIntake.addEventListener("click", () => this.session.startNewIntake());
    dom.clearStorage.addEventListener("click", () => this.onClearStorage());
    for (const button of win.document.querySelectorAll("[data-copy]")) {
      button.addEventListener("click", () => this.onCopyDemo(button));
    }
    dom.startRecording.addEventListener("click", () => this.voice.startRecording());
    dom.stopRecording.addEventListener("click", () => this.voice.stopRecording());
    dom.wavAttach.addEventListener("click", () => {
      if (!dom.wavAttach.disabled) dom.wav.click();
    });
    dom.wav.addEventListener("change", () => this.voice.onWavSelected());
    dom.verify.addEventListener("click", () => this.turns.onVerifyOrder());
    dom.postcode.addEventListener("input", () => this.view.updateControls());
    dom.code.addEventListener("input", () => this.view.updateControls());
    dom.message.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.turns.onSend();
      }
    });
    dom.message.addEventListener("input", () => this.onMessageInput());
    win.addEventListener("pagehide", () => this.onPageHide());
  }

  async onCopyDemo(button) {
    const { document, navigator } = this.window;
    const value = document.getElementById(button.dataset.copy).textContent;
    const status = document.getElementById("demo-copy-status");
    status.hidden = false;
    status.textContent = "";
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        // Clipboard API needs HTTPS or localhost; the demo also runs on HTTP LAN addresses.
        const field = document.createElement("textarea");
        field.value = value;
        field.readOnly = true;
        field.className = "u-sr-only";
        const focused = document.activeElement;
        document.body.append(field);
        try {
          field.select();
          if (!document.execCommand?.("copy")) throw new Error("Clipboard unavailable");
        } finally {
          field.remove();
          focused?.focus();
        }
      }
      // Success is announced to screen readers only; a failure has to be seen.
      status.classList.add("u-sr-only");
      this.markCopied(button);
      status.textContent = this.i18n.t(this.state.language, "demo.copied", { value });
    } catch (_) {
      status.classList.remove("u-sr-only");
      button.classList.remove("is-copied");
      status.textContent = this.i18n.t(this.state.language, "demo.copy_failed");
    }
  }

  /** The pressed copy button shows a tick for a moment; only one shows it at a time. */
  markCopied(button) {
    for (const other of this.window.document.querySelectorAll("[data-copy]")) {
      other.classList.remove("is-copied");
    }
    button.classList.add("is-copied");
    this.window.clearTimeout(this.copiedTimer);
    this.copiedTimer = this.window.setTimeout(() => button.classList.remove("is-copied"), COPIED_MS);
  }

  /** The test card's reset: forget everything this page stored in the browser, then start afresh. */
  onClearStorage() {
    try {
      this.window.localStorage.clear();
    } catch (_) {
      /* storage blocked: the new conversation still starts */
    }
    return this.session.startNewIntake();
  }

  onMessageInput() {
    const { state } = this;
    // Editing a failed draft cancels its retry payload.
    if (state.retryKind === "turn") this.presenter.clearRetry();
    // Typing after a send is text unless a transcript draft is still in the box.
    if (state.inputMode === "transcript" && !this.view.draftNonempty()) state.inputMode = "text";
    this.view.updateControls();
  }

  onPageHide() {
    this.state.generation += 1;
    this.state.speechPending = null;
    this.voice.discardRecording();
    this.player.pause();
    this.player.release();
  }
}
