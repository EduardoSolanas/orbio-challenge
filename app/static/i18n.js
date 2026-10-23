(() => {
  "use strict";

  const LANGUAGES = Object.freeze(["en", "es"]);

  const STRINGS = {
    en: {
      title: "Northstar support intake",
      reviewRegion: "Review details",
      messageLabel: "Message",
      send: "Send",
      retry: "Retry",
      confirmDetails: "Confirm details",
      newIntake: "New intake",
      startRecording: "Start recording",
      stopRecording: "Stop recording",
      transcribeAudio: "Transcribe audio",
      wavRecording: "WAV recording",
      transcribeHelp: "Send or clear your current draft before transcribing.",
      playbackDisclosure: "Voice is AI-generated",
      playReply: "Play reply",
      playReplyNumbered: "Play reply {number}",
      sources: "Sources",
      knowledgeBaseVersion: "Knowledge base version: {version}",
      you: "You",
      assistant: "Assistant",
      speechUnavailable:
        "Spoken playback is not available for this reply's language.",
      charLimit: "Messages must contain at most 4000 characters.",
      micDenied:
        "Microphone access was denied or is unavailable. You can type or upload a WAV recording.",
      timeout: "The AI request timed out. Please retry.",
      network: "The AI service is unavailable. Please retry.",
      playbackFallback:
        "Audio could not play; your text reply is still available.",
      genericError: "An internal error occurred.",
    },
    es: {
      title: "Registro de soporte de Northstar",
      reviewRegion: "Revisar datos",
      messageLabel: "Mensaje",
      send: "Enviar",
      retry: "Reintentar",
      confirmDetails: "Confirmar datos",
      newIntake: "Nuevo registro",
      startRecording: "Empezar a grabar",
      stopRecording: "Detener grabación",
      transcribeAudio: "Transcribir audio",
      wavRecording: "Grabación WAV",
      transcribeHelp:
        "Envía o borra tu borrador actual antes de transcribir.",
      playbackDisclosure: "La voz es generada por IA",
      playReply: "Reproducir respuesta",
      playReplyNumbered: "Reproducir respuesta {number}",
      sources: "Fuentes",
      knowledgeBaseVersion: "Versión de la base de conocimiento: {version}",
      you: "Tú",
      assistant: "Asistente",
      speechUnavailable:
        "La reproducción de voz no está disponible en el idioma de esta respuesta.",
      charLimit: "Los mensajes deben tener como máximo 4000 caracteres.",
      micDenied:
        "Se denegó el acceso al micrófono o no está disponible. Puedes escribir o subir una grabación WAV.",
      timeout: "La solicitud de IA agotó el tiempo de espera. Inténtalo de nuevo.",
      network: "El servicio de IA no está disponible. Inténtalo de nuevo.",
      playbackFallback:
        "No se pudo reproducir el audio; la respuesta de texto sigue disponible.",
      genericError: "Se produjo un error interno.",
    },
  };

  const ERROR_MESSAGES = {
    en: {
      session_not_found: "Session not found.",
      message_not_found: "Assistant message not found.",
      revision_conflict: "This session changed. Refresh before sending again.",
      request_id_conflict: "This request ID was already used for different input.",
      stale_review:
        "The review changed. Review the latest details before confirming.",
      session_completed:
        "This intake is complete. Start a new intake to make changes.",
      turn_limit:
        "This intake has reached its message limit. You can confirm an existing review or start a new intake.",
      review_not_ready: "Resolve missing or unclear details before confirming.",
      input_too_large: "The input exceeds the allowed size.",
      unsupported_audio: "Upload a supported PCM WAV recording.",
      invalid_request: "The request does not match the expected format.",
      invalid_audio: "The WAV recording is invalid or exceeds the audio limits.",
      silent_audio:
        "No speech was detected. Try another recording or type your message.",
      empty_transcription:
        "No transcript was returned. Try another recording or type your message.",
      transcript_too_long:
        "The transcript exceeds 4000 characters. Use a shorter recording.",
      provider_busy: "The AI service is busy. Please retry.",
      session_busy: "This session is processing another request. Please retry.",
      invalid_provider_response:
        "The AI service returned an unusable response. Please retry.",
      provider_unavailable: "The AI service is unavailable. Please retry.",
      provider_configuration:
        "Spoken playback is not available for this reply's language. The text reply is still available.",
      storage_unavailable:
        "Session storage is unavailable. Please retry.",
      storage_corrupt:
        "This session cannot be loaded. Contact the demo operator.",
      provider_timeout: "The AI request timed out. Please retry.",
      internal_error: "An internal error occurred.",
    },
    es: {
      session_not_found: "No se encontró la sesión.",
      message_not_found: "No se encontró el mensaje del asistente.",
      revision_conflict:
        "Esta sesión ha cambiado. Actualiza antes de volver a enviar.",
      request_id_conflict:
        "Este ID de solicitud ya se usó con datos diferentes.",
      stale_review:
        "La revisión ha cambiado. Revisa los últimos datos antes de confirmar.",
      session_completed:
        "Este registro está completo. Inicia un registro nuevo para hacer cambios.",
      turn_limit:
        "Este registro ha alcanzado su límite de mensajes. Puedes confirmar una revisión existente o iniciar un registro nuevo.",
      review_not_ready:
        "Resuelve los datos faltantes o poco claros antes de confirmar.",
      input_too_large: "La entrada supera el tamaño permitido.",
      unsupported_audio: "Sube una grabación PCM WAV compatible.",
      invalid_request: "La solicitud no coincide con el formato esperado.",
      invalid_audio:
        "La grabación WAV no es válida o supera los límites de audio.",
      silent_audio:
        "No se detectó voz. Prueba otra grabación o escribe tu mensaje.",
      empty_transcription:
        "No se devolvió ninguna transcripción. Prueba otra grabación o escribe tu mensaje.",
      transcript_too_long:
        "La transcripción supera los 4000 caracteres. Usa una grabación más corta.",
      provider_busy: "El servicio de IA está ocupado. Inténtalo de nuevo.",
      session_busy:
        "Esta sesión está procesando otra solicitud. Inténtalo de nuevo.",
      invalid_provider_response:
        "El servicio de IA devolvió una respuesta no utilizable. Inténtalo de nuevo.",
      provider_unavailable:
        "El servicio de IA no está disponible. Inténtalo de nuevo.",
      provider_configuration:
        "La reproducción de voz no está disponible en el idioma de esta respuesta. La respuesta de texto sigue disponible.",
      storage_unavailable:
        "El almacenamiento de sesiones no está disponible. Inténtalo de nuevo.",
      storage_corrupt:
        "No se puede cargar esta sesión. Contacta con el operador de la demo.",
      provider_timeout:
        "La solicitud de IA agotó el tiempo de espera. Inténtalo de nuevo.",
      internal_error: "Se produjo un error interno.",
    },
  };

  const STATIC_LABELS = Object.freeze([
    ["app-title", "title", "text"],
    ["review", "reviewRegion", "aria"],
    ["message", "messageLabel", "aria"],
    ["message-label", "messageLabel", "text"],
    ["transcribe-help", "transcribeHelp", "text"],
    ["send", "send", "text"],
    ["retry", "retry", "text"],
    ["confirm", "confirmDetails", "text"],
    ["new-intake", "newIntake", "text"],
    ["start-recording", "startRecording", "text"],
    ["stop-recording", "stopRecording", "text"],
    ["transcribe", "transcribeAudio", "text"],
    ["wav-label", "wavRecording", "text"],
    ["wav-recording", "wavRecording", "aria"],
  ]);

  function languageOf(value) {
    return value === "es" ? "es" : "en";
  }

  function format(template, values) {
    return template.replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(values, name)
        ? String(values[name])
        : match
    );
  }

  function t(lang, key, values) {
    const table = STRINGS[languageOf(lang)];
    if (!Object.prototype.hasOwnProperty.call(table, key)) {
      throw new Error(`missing i18n key: ${key}`);
    }
    const template = table[key];
    return values ? format(template, values) : template;
  }

  function translateError(code, lang) {
    const table = ERROR_MESSAGES[languageOf(lang)];
    if (code && Object.prototype.hasOwnProperty.call(table, code)) {
      return table[code];
    }
    return t(lang, "genericError");
  }

  function applyStaticLabels(lang, doc) {
    const language = languageOf(lang);
    const documentRef = doc || (typeof document !== "undefined" ? document : null);
    if (!documentRef) return language;
    documentRef.documentElement.lang = language;
    for (const [id, key, mode] of STATIC_LABELS) {
      const element = documentRef.getElementById(id);
      if (!element) continue;
      if (mode === "aria") element.setAttribute("aria-label", t(language, key));
      else element.textContent = t(language, key);
    }
    const disclosure = documentRef.querySelector(".playback__disclosure");
    if (disclosure) disclosure.textContent = t(language, "playbackDisclosure");
    return language;
  }

  const api = Object.freeze({
    LANGUAGES,
    STRINGS,
    ERROR_MESSAGES,
    languageOf,
    t,
    translateError,
    applyStaticLabels,
  });

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.NorthstarI18n = api;
})();
