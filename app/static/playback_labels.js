(() => {
  "use strict";

  const LABEL = "Play reply";

  function playbackLabel() {
    return LABEL;
  }

  function playbackAccessibleName(number) {
    return `${LABEL} ${number}`;
  }

  const api = Object.freeze({ playbackLabel, playbackAccessibleName });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.NorthstarPlaybackLabels = api;
})();
