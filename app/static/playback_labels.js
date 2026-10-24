(() => {
  "use strict";

  const LABELS = Object.freeze({
    en: "Play reply",
    es: "Reproducir respuesta",
  });

  function languageOf(language) {
    return language === "es" ? "es" : "en";
  }

  function playbackLabel(language) {
    return LABELS[languageOf(language)];
  }

  function playbackAccessibleName(number, language) {
    return `${playbackLabel(language)} ${number}`;
  }

  const api = Object.freeze({ playbackLabel, playbackAccessibleName });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.NorthstarPlaybackLabels = api;
})();
