(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.OrbioRecording = api;
})(typeof globalThis === "object" ? globalThis : this, () => {
  const MAX_RECORD_SECONDS = 60;

  class RecordingAccumulator {
    constructor(sampleRate) {
      this.maxFrames = Math.floor(sampleRate * MAX_RECORD_SECONDS);
      this.chunks = [];
      this.frameCount = 0;
    }

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

  return { MAX_RECORD_SECONDS, RecordingAccumulator };
});
