import { MAX_RECORD_SECONDS, MAX_WAV_BYTES } from "./config.js";
import { Controller } from "./controller.js";
import { MicrophoneRecording, SILENCE_GRACE_MS } from "./microphone.js";
import { RecordingAccumulator, WavEncoder } from "./recording.js";

/** Spoken replies, microphone recording and transcription. */
export class VoiceController extends Controller {
  constructor({ player, gate, window: win, ...rest }) {
    super(rest);
    this.player = player;
    this.gate = gate;
    this.window = win;
  }

  /** The reply was superseded: a new conversation started or another reply began playing. */
  isStale(gen, token) {
    return gen !== this.state.generation || token !== this.state.playbackToken;
  }

  async playReply(messageId) {
    const { state, view, api, player } = this;
    if (!state.sessionId) return;
    if (state.speechPending && state.speechPending.generation === state.generation) {
      if (state.speechPending.messageId === messageId) return;
      state.bumpPlayback();
    }
    const gen = state.generation;
    const token = state.bumpPlayback();
    state.speechPending = { generation: gen, messageId, token };
    view.updateSpeechButtons();
    try {
      const response = await api.postSpeech(state.sessionId, messageId);
      if (this.isStale(gen, token)) return;
      if (!response.ok) {
        const err = await api.parseError(response);
        view.showAlert(err.message);
        return;
      }
      const blob = await response.blob();
      if (this.isStale(gen, token)) return;
      if (!blob.size || (blob.type && !blob.type.startsWith("audio/"))) {
        view.showAlert(this.ui("ui.playback_fallback"));
        return;
      }
      const url = player.createUrl(blob);
      if (this.isStale(gen, token)) {
        player.revokeUrl(url);
        return;
      }
      player.release();
      if (this.isStale(gen, token)) {
        player.revokeUrl(url);
        return;
      }
      player.use(url);
      try {
        await player.play();
      } catch (_) {
        if (!this.isStale(gen, token)) view.showAlert(this.ui("ui.playback_fallback"));
      }
    } catch (error) {
      if (this.isStale(gen, token)) return;
      this.alertTransportError(error);
    } finally {
      const pending = state.speechPending;
      if (
        pending &&
        pending.generation === gen &&
        pending.messageId === messageId &&
        pending.token === token
      ) {
        state.speechPending = null;
        view.updateSpeechButtons();
      }
    }
  }

  discardRecording() {
    const { state, view } = this;
    if (!state.recording) return;
    const recording = state.recording;
    recording.discard();
    if (state.recording === recording) state.recording = null;
    view.updateControls();
  }

  async onTranscribe() {
    const { state, view, api, gate } = this;
    if (state.sttPending || !state.audioBlob || view.draftBlocksVoice()) return;
    if (!gate.tryAcquire()) return;
    const gen = state.generation;
    const controller = new AbortController();
    state.sttAbort = controller;
    state.sttPending = true;
    view.updateControls();
    view.clearAlert();
    try {
      const response = await api.postTranscription(state.audioBlob, controller);
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await api.parseError(response);
        if (gen === state.generation) {
          state.audioBlob = null;
          view.showAlert(err.message);
        }
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      view.dom.message.value = data && typeof data.text === "string" ? data.text : "";
      state.inputMode = "transcript";
      state.audioBlob = null;
      if (view.dom.wav) view.dom.wav.value = "";
      view.clearAlert();
    } catch (error) {
      if (gen !== state.generation) return;
      state.audioBlob = null;
      view.showAlert(
        error && error.name === "AbortError"
          ? this.ui("errors.provider_timeout")
          : this.ui("errors.provider_unavailable")
      );
    } finally {
      gate.release();
      if (gen === state.generation) {
        state.sttPending = false;
        state.sttAbort = null;
        view.updateControls();
      }
    }
  }

  async startRecording() {
    const { state, view } = this;
    if (state.recording || state.recordingPending || state.sttPending || view.draftBlocksVoice()) return;
    view.clearAlert();
    const mediaDevices = this.window.navigator.mediaDevices;
    if (!mediaDevices || !mediaDevices.getUserMedia) {
      view.showAlert(this.ui("ui.mic_denied"));
      return;
    }
    const gen = state.generation;
    let stream = null;
    let ctx = null;
    state.recordingPending = true;
    view.updateControls();
    try {
      stream = await mediaDevices.getUserMedia({ audio: true });
      // A new intake or page exit may have happened while permission was pending.
      if (gen !== state.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const AudioCtx = this.window.AudioContext || this.window.webkitAudioContext;
      ctx = new AudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      if (gen !== state.generation) {
        stream.getTracks().forEach((track) => track.stop());
        try {
          ctx.close();
        } catch (_) {
          /* ignore */
        }
        return;
      }
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      const accumulator = new RecordingAccumulator(ctx.sampleRate);
      const recording = new MicrophoneRecording({ stream, ctx, processor, source, accumulator });
      processor.onaudioprocess = (event) => {
        if (recording.stopped) return;
        if (!accumulator.append(event.inputBuffer.getChannelData(0))) {
          this.stopRecording({ force: true });
        }
      };
      source.connect(processor);
      processor.connect(ctx.destination);
      recording.addTimer(setTimeout(() => this.stopRecording({ force: true }), MAX_RECORD_SECONDS * 1000));
      state.recording = recording;
      view.dom.stopRecording.hidden = false;
    } catch (_) {
      if (stream) stream.getTracks().forEach((track) => track.stop());
      if (ctx) {
        try {
          ctx.close();
        } catch (_closeError) {
          /* ignore */
        }
      }
      if (gen === state.generation) view.showAlert(this.ui("ui.mic_denied"));
    } finally {
      if (gen === state.generation) {
        state.recordingPending = false;
        view.updateControls();
      }
    }
  }

  stopRecording({ force = false } = {}) {
    const { state, view } = this;
    const recording = state.recording;
    if (!recording || recording.stopped) return;
    // A fake input device starts with a short silent handshake. Keep the live graph running
    // briefly so a real take is not misclassified as silent.
    if (!force && !recording.hasSignal() && !recording.waitingForSignal) {
      recording.waitingForSignal = true;
      const timer = setTimeout(() => {
        recording.timers.delete(timer);
        this.stopRecording();
      }, SILENCE_GRACE_MS);
      recording.addTimer(timer);
      return;
    }
    const { samples, sampleRate } = recording.finish();
    state.recording = null;
    const blob = WavEncoder.encode(samples, sampleRate);
    if (blob.size > MAX_WAV_BYTES) {
      view.showAlert(this.ui("errors.invalid_audio"));
      state.audioBlob = null;
    } else {
      state.audioBlob = blob;
    }
    view.dom.stopRecording.hidden = true;
    view.updateControls();
    if (state.audioBlob) void this.onTranscribe();
  }

  onWavSelected() {
    const { state, view } = this;
    const file = view.dom.wav.files && view.dom.wav.files[0];
    state.audioBlob = file || null;
    view.dom.wav.value = "";
    view.updateControls();
    if (state.audioBlob) void this.onTranscribe();
  }
}
