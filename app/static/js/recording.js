import { MAX_RECORD_SECONDS } from "./config.js";

/** A single-slot lock: only one transcription may run at a time. */
export class AsyncGate {
  constructor() {
    this.locked = false;
  }

  tryAcquire() {
    if (this.locked) return false;
    this.locked = true;
    return true;
  }

  release() {
    this.locked = false;
  }
}

/** Collects microphone chunks up to the recording limit. */
export class RecordingAccumulator {
  constructor(sampleRate) {
    this.maxFrames = Math.floor(sampleRate * MAX_RECORD_SECONDS);
    this.chunks = [];
    this.frameCount = 0;
  }

  /** Store a chunk; false once the limit is reached and recording should stop. */
  append(input) {
    const remaining = this.maxFrames - this.frameCount;
    if (remaining <= 0) return false;
    const frames = Math.min(remaining, input.length);
    this.chunks.push(new Float32Array(input.slice(0, frames)));
    this.frameCount += frames;
    return this.frameCount < this.maxFrames;
  }

  toSamples(fadeSamples = 0) {
    const samples = new Float32Array(this.frameCount);
    let offset = 0;
    for (const chunk of this.chunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    const fadeLength = Math.min(Math.max(0, fadeSamples), samples.length);
    for (let i = 0; i < fadeLength; i += 1) {
      samples[samples.length - fadeLength + i] *= (fadeLength - 1 - i) / fadeLength;
    }
    return samples;
  }
}

/** Encodes mono float samples as a 16-bit PCM WAV blob. */
export class WavEncoder {
  static encode(float32Samples, sampleRate) {
    const numFrames = float32Samples.length;
    const buffer = new ArrayBuffer(44 + numFrames * 2);
    const view = new DataView(buffer);
    const writeStr = (offset, str) => {
      for (let i = 0; i < str.length; i += 1) {
        view.setUint8(offset + i, str.charCodeAt(i));
      }
    };
    writeStr(0, "RIFF");
    view.setUint32(4, 36 + numFrames * 2, true);
    writeStr(8, "WAVE");
    writeStr(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, "data");
    view.setUint32(40, numFrames * 2, true);
    let offset = 44;
    for (let i = 0; i < numFrames; i += 1) {
      const sample = Math.max(-1, Math.min(1, float32Samples[i]));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      offset += 2;
    }
    return new Blob([buffer], { type: "audio/wav" });
  }
}
