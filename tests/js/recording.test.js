/** @jest-environment node */
import { describe, expect, test } from "@jest/globals";

import { MAX_RECORD_SECONDS } from "../../gateway/static/js/config.js";
import { AsyncGate, RecordingAccumulator, WavEncoder } from "../../gateway/static/js/recording.js";

const SUPPORTED_SAMPLE_RATES = [16000, 24000, 44100, 48000];

describe("RecordingAccumulator", () => {
  test.each(SUPPORTED_SAMPLE_RATES)("trims an overrun final callback at the frame boundary (%i Hz)", (rate) => {
    const accumulator = new RecordingAccumulator(rate);
    const exactFrames = rate * MAX_RECORD_SECONDS;
    const first = Float32Array.from({ length: exactFrames - 1 }, (_, index) => index / exactFrames);

    expect(accumulator.append(first)).toBe(true);
    expect(accumulator.append(Float32Array.from([0.25, 0.5]))).toBe(false);
    expect(accumulator.frameCount).toBe(exactFrames);
    const samples = accumulator.toSamples();
    expect(samples.at(-2)).toBeCloseTo((exactFrames - 2) / exactFrames, 6);
    expect(samples.at(-1)).toBe(0.25);
  });

  test("does not append another callback after the cutoff", () => {
    const accumulator = new RecordingAccumulator(16000);
    accumulator.append(new Float32Array(16000 * MAX_RECORD_SECONDS));

    expect(accumulator.append(Float32Array.from([1, 2, 3]))).toBe(false);
    expect(accumulator.frameCount).toBe(16000 * MAX_RECORD_SECONDS);
  });

  test("applies a short fade without changing the frame count", () => {
    const accumulator = new RecordingAccumulator(16000);
    accumulator.append(Float32Array.from({ length: 16000 * MAX_RECORD_SECONDS }, () => 1));

    const samples = accumulator.toSamples(80);
    expect(samples).toHaveLength(16000 * MAX_RECORD_SECONDS);
    expect(samples.at(-81)).toBe(1);
    expect(samples.at(-1)).toBe(0);
    expect(samples.at(-40)).toBeGreaterThan(0);
    expect(samples.at(-40)).toBeLessThan(1);
  });

  test("joins chunks in order and ignores a fade longer than the clip", () => {
    const accumulator = new RecordingAccumulator(16000);
    accumulator.append(Float32Array.from([0.1, 0.2]));
    accumulator.append(Float32Array.from([0.3]));

    expect([...accumulator.toSamples(0)]).toEqual([
      expect.closeTo(0.1, 6),
      expect.closeTo(0.2, 6),
      expect.closeTo(0.3, 6),
    ]);
    expect(accumulator.toSamples(-5)).toHaveLength(3);
    expect(accumulator.toSamples(1000).at(-1)).toBe(0);
  });

  test("copies each chunk so later changes to the input do not alter the recording", () => {
    const accumulator = new RecordingAccumulator(16000);
    const input = Float32Array.from([0.5, 0.5]);
    accumulator.append(input);
    input.fill(0);

    expect(accumulator.toSamples()[0]).toBe(0.5);
  });
});

describe("WavEncoder", () => {
  test("encodes recorder samples as mono 16-bit PCM WAV", async () => {
    const blob = WavEncoder.encode(Float32Array.from([-1, 0, 1]), 16000);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const view = new DataView(bytes.buffer);
    const ascii = (start, end) => String.fromCharCode(...bytes.slice(start, end));

    expect(blob.type).toBe("audio/wav");
    expect(bytes).toHaveLength(50);
    expect(ascii(0, 4)).toBe("RIFF");
    expect(view.getUint32(4, true)).toBe(42);
    expect(ascii(8, 12)).toBe("WAVE");
    expect(ascii(12, 16)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(ascii(36, 40)).toBe("data");
    expect(view.getUint32(40, true)).toBe(6);
    expect(view.getInt16(44, true)).toBe(-32768);
    expect(view.getInt16(46, true)).toBe(0);
    expect(view.getInt16(48, true)).toBe(32767);
  });

  test("clips samples outside the -1 to 1 range", async () => {
    const blob = WavEncoder.encode(Float32Array.from([-3, 3]), 8000);
    const view = new DataView(await blob.arrayBuffer());

    expect(view.getInt16(44, true)).toBe(-32768);
    expect(view.getInt16(46, true)).toBe(32767);
  });
});

describe("AsyncGate", () => {
  test("allows only one in-flight action and can be reused after release", () => {
    const gate = new AsyncGate();

    expect(gate.tryAcquire()).toBe(true);
    expect(gate.tryAcquire()).toBe(false);
    gate.release();
    expect(gate.tryAcquire()).toBe(true);
  });
});
