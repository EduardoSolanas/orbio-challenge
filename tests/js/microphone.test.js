import { beforeEach, describe, expect, jest, test } from "@jest/globals";

import {
  FADE_SECONDS,
  MicrophoneRecording,
  SIGNAL_FLOOR,
  SILENCE_GRACE_MS,
} from "../../web/js/microphone.js";
import { RecordingAccumulator } from "../../web/js/recording.js";

function build({ sampleRate = 16000 } = {}) {
  const track = { stop: jest.fn() };
  const parts = {
    track,
    stream: { getTracks: () => [track] },
    ctx: { sampleRate, close: jest.fn() },
    processor: { disconnect: jest.fn() },
    source: { disconnect: jest.fn() },
    accumulator: new RecordingAccumulator(sampleRate),
  };
  return { ...parts, recording: new MicrophoneRecording(parts) };
}

describe("signal detection", () => {
  test("the floor is 128 on a 16-bit scale and the grace period is a quarter second", () => {
    expect(SIGNAL_FLOOR).toBe(128 / 32768);
    expect(SILENCE_GRACE_MS).toBe(250);
    expect(FADE_SECONDS).toBe(0.005);
  });

  test("silence is not a signal", () => {
    const { recording, accumulator } = build();
    accumulator.append(new Float32Array(100));

    expect(recording.hasSignal()).toBe(false);
  });

  test("a sample at the floor counts as a signal, one just under does not", () => {
    const quiet = build();
    quiet.accumulator.append(Float32Array.from([SIGNAL_FLOOR / 2, -SIGNAL_FLOOR / 2]));
    const loud = build();
    loud.accumulator.append(Float32Array.from([0, -SIGNAL_FLOOR]));

    expect(quiet.recording.hasSignal()).toBe(false);
    expect(loud.recording.hasSignal()).toBe(true);
  });

  test("chunks come from the accumulator", () => {
    const { recording, accumulator } = build();
    accumulator.append(Float32Array.from([0.1]));

    expect(recording.chunks).toBe(accumulator.chunks);
  });
});

describe("timers", () => {
  beforeEach(() => jest.useFakeTimers());

  test("clearTimers cancels every registered timer", () => {
    const { recording } = build();
    const fired = jest.fn();
    recording.addTimer(setTimeout(fired, 100));
    recording.addTimer(setInterval(fired, 50));

    recording.clearTimers();
    jest.advanceTimersByTime(1000);

    expect(fired).not.toHaveBeenCalled();
    expect(recording.timers.size).toBe(0);
  });

  test("addTimer returns the timer it was given", () => {
    const { recording } = build();
    const timer = setTimeout(() => {}, 10);

    expect(recording.addTimer(timer)).toBe(timer);
    recording.clearTimers();
  });
});

describe("stopping", () => {
  test("discard stops the tracks, disconnects the graph and closes the context", () => {
    const { recording, track, ctx, processor, source } = build();

    recording.discard();

    expect(recording.stopped).toBe(true);
    expect(track.stop).toHaveBeenCalled();
    expect(processor.disconnect).toHaveBeenCalled();
    expect(source.disconnect).toHaveBeenCalled();
    expect(ctx.close).toHaveBeenCalled();
  });

  test("discard survives a graph that refuses to disconnect or a context that refuses to close", () => {
    const { recording, track, ctx, processor } = build();
    processor.disconnect.mockImplementation(() => {
      throw new Error("already disconnected");
    });
    ctx.close.mockImplementation(() => {
      throw new Error("already closed");
    });

    expect(() => recording.discard()).not.toThrow();
    expect(track.stop).toHaveBeenCalled();
  });

  test("finish returns the recorded samples with a short fade, then releases everything", () => {
    const { recording, accumulator, ctx, track } = build({ sampleRate: 16000 });
    accumulator.append(Float32Array.from({ length: 1000 }, () => 1));

    const { samples, sampleRate } = recording.finish();

    expect(sampleRate).toBe(16000);
    expect(samples).toHaveLength(1000);
    expect(samples[0]).toBe(1);
    expect(samples.at(-1)).toBe(0);
    expect(recording.stopped).toBe(true);
    expect(track.stop).toHaveBeenCalled();
    expect(ctx.close).toHaveBeenCalled();
  });
});
