/** A sample counts as speech from about this amplitude (128 on a 16-bit scale). */
export const SIGNAL_FLOOR = 128 / 32768;
/** How long a take with no signal yet is kept running before it is judged silent. */
export const SILENCE_GRACE_MS = 250;
/** The audio tail that is faded out so the clip does not end with a click. */
export const FADE_SECONDS = 0.005;

/** One live microphone capture: the audio graph, its timers and what was recorded. */
export class MicrophoneRecording {
  constructor({ stream, ctx, processor, source, accumulator }) {
    this.stream = stream;
    this.ctx = ctx;
    this.processor = processor;
    this.source = source;
    this.accumulator = accumulator;
    this.stopped = false;
    this.waitingForSignal = false;
    this.timers = new Set();
  }

  get chunks() {
    return this.accumulator.chunks;
  }

  addTimer(timer) {
    this.timers.add(timer);
    return timer;
  }

  clearTimers() {
    for (const timer of this.timers) {
      clearTimeout(timer);
      clearInterval(timer);
    }
    this.timers.clear();
  }

  hasSignal() {
    return this.chunks.some((chunk) => {
      for (let i = 0; i < chunk.length; i += 1) {
        if (Math.abs(chunk[i]) >= SIGNAL_FLOOR) return true;
      }
      return false;
    });
  }

  release() {
    try {
      this.processor.disconnect();
      this.source.disconnect();
    } catch (_) {
      /* ignore */
    }
    this.stream.getTracks().forEach((track) => track.stop());
  }

  closeContext() {
    try {
      this.ctx.close();
    } catch (_) {
      /* ignore */
    }
  }

  /** Stop everything and throw the recording away. */
  discard() {
    this.stopped = true;
    this.clearTimers();
    this.release();
    this.closeContext();
  }

  /** Stop everything and return what was recorded. */
  finish() {
    this.clearTimers();
    this.stopped = true;
    this.release();
    const sampleRate = this.ctx.sampleRate;
    const samples = this.accumulator.toSamples(Math.floor(sampleRate * FADE_SECONDS));
    this.closeContext();
    return { samples, sampleRate };
  }
}
