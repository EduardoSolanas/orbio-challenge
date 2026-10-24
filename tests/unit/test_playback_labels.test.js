const test = require("node:test");
const assert = require("node:assert/strict");
const { playbackLabel, playbackAccessibleName } = require("../../app/static/playback_labels.js");

test("playback labels are localized without exposing the message identifier", () => {
  assert.equal(playbackLabel("en"), "Play reply");
  assert.equal(playbackLabel("es"), "Reproducir respuesta");
});

test("playback accessible names include the one-based reply number", () => {
  assert.equal(playbackAccessibleName(2, "en"), "Play reply 2");
  assert.equal(playbackAccessibleName(2, "es"), "Reproducir respuesta 2");
});
