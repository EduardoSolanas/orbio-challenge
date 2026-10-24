const test = require("node:test");
const assert = require("node:assert/strict");
const {
  MAX_RECORD_SECONDS,
  RecordingAccumulator,
  AsyncGate,
  encodeWav,
} = require("../../app/static/recording.js");

const SUPPORTED_SAMPLE_RATES = [16000, 24000, 44100, 48000];

test("trims an overrun final callback at the frame boundary for every supported rate", () => {
  for (const sampleRate of SUPPORTED_SAMPLE_RATES) {
    const accumulator = new RecordingAccumulator(sampleRate);
    const exactFrames = sampleRate * MAX_RECORD_SECONDS;
    const first = Float32Array.from({ length: exactFrames - 1 }, (_, index) => index / exactFrames);
    const final = Float32Array.from([0.25, 0.5]);

    assert.equal(accumulator.append(first), true);
    assert.equal(accumulator.append(final), false);
    assert.equal(accumulator.frameCount, exactFrames);
    const samples = accumulator.toSamples();
    assert.ok(Math.abs(samples.at(-2) - ((exactFrames - 2) / exactFrames)) < 1e-6);
    assert.equal(samples.at(-1), 0.25);
  }
});

test("does not append another callback after the cutoff", () => {
  const accumulator = new RecordingAccumulator(16000);
  accumulator.append(new Float32Array(16000 * MAX_RECORD_SECONDS));

  assert.equal(accumulator.append(Float32Array.from([1, 2, 3])), false);
  assert.equal(accumulator.frameCount, 16000 * MAX_RECORD_SECONDS);
});

test("applies a short fade without changing the frame count", () => {
  const accumulator = new RecordingAccumulator(16000);
  accumulator.append(Float32Array.from({ length: 16000 * MAX_RECORD_SECONDS }, () => 1));

  const samples = accumulator.toSamples(80);
  assert.equal(samples.length, 16000 * MAX_RECORD_SECONDS);
  assert.equal(samples.at(-81), 1);
  assert.equal(samples.at(-1), 0);
  assert.ok(samples.at(-40) > 0 && samples.at(-40) < 1);
});

test("encodes recorder samples as mono 16-bit PCM WAV", async () => {
  const blob = encodeWav(Float32Array.from([-1, 0, 1]), 16000);
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const view = new DataView(bytes.buffer);
  const ascii = (start, end) => String.fromCharCode(...bytes.slice(start, end));

  assert.equal(blob.type, "audio/wav");
  assert.equal(bytes.length, 50);
  assert.equal(ascii(0, 4), "RIFF");
  assert.equal(view.getUint32(4, true), 42);
  assert.equal(ascii(8, 12), "WAVE");
  assert.equal(ascii(12, 16), "fmt ");
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(ascii(36, 40), "data");
  assert.equal(view.getUint32(40, true), 6);
  assert.equal(view.getInt16(44, true), -32768);
  assert.equal(view.getInt16(46, true), 0);
  assert.equal(view.getInt16(48, true), 32767);
});

test("allows only one in-flight action and can be reused after release", () => {
  const gate = new AsyncGate();

  assert.equal(gate.tryAcquire(), true);
  assert.equal(gate.tryAcquire(), false);
  gate.release();
  assert.equal(gate.tryAcquire(), true);
  gate.release();
});
