import { beforeEach, describe, expect, jest, test } from "@jest/globals";

import { AudioPlayer } from "../../app/static/js/audio_player.js";
import { ChatState } from "../../app/static/js/state.js";

function build() {
  const element = {
    pause: jest.fn(),
    load: jest.fn(),
    play: jest.fn(() => Promise.resolve("playing")),
    removeAttribute: jest.fn(),
    src: "",
  };
  const urls = { createObjectURL: jest.fn(() => "blob:one"), revokeObjectURL: jest.fn() };
  const state = new ChatState();
  return { element, urls, state, player: new AudioPlayer({ element, state, urls }) };
}

describe("AudioPlayer", () => {
  let parts;

  beforeEach(() => {
    parts = build();
  });

  test("use() makes a url the current source", () => {
    parts.player.use("blob:one");

    expect(parts.state.objectUrl).toBe("blob:one");
    expect(parts.element.src).toBe("blob:one");
  });

  test("release() stops playback and frees the current url", () => {
    parts.player.use("blob:one");

    parts.player.release();

    expect(parts.element.pause).toHaveBeenCalled();
    expect(parts.urls.revokeObjectURL).toHaveBeenCalledWith("blob:one");
    expect(parts.state.objectUrl).toBeNull();
    expect(parts.element.removeAttribute).toHaveBeenCalledWith("src");
    expect(parts.element.load).toHaveBeenCalled();
  });

  test("release() with nothing playing only clears the element", () => {
    parts.player.release();

    expect(parts.urls.revokeObjectURL).not.toHaveBeenCalled();
    expect(parts.element.removeAttribute).toHaveBeenCalledWith("src");
  });

  test("release() still frees the url when pausing throws", () => {
    parts.element.pause.mockImplementation(() => {
      throw new Error("not supported");
    });
    parts.player.use("blob:one");

    parts.player.release();

    expect(parts.urls.revokeObjectURL).toHaveBeenCalledWith("blob:one");
  });

  test("createUrl, revokeUrl and play delegate to the browser", async () => {
    expect(parts.player.createUrl({ size: 1 })).toBe("blob:one");
    parts.player.revokeUrl("blob:one");

    expect(parts.urls.revokeObjectURL).toHaveBeenCalledWith("blob:one");
    expect(await parts.player.play()).toBe("playing");
  });
});
