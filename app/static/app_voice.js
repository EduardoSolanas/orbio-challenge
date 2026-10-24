(() => {
  "use strict";

  const app = window.NorthstarApp;
  const {
    MAX_WAV_BYTES,
    MAX_RECORD_SECONDS,
    DEADLINE_STT_MS,
    DEADLINE_TTS_MS,
    ui,
    els,
    state,
    transcriptionGate,
    encodeWav,
    showAlert,
    clearAlert,
    revokeAudioUrl,
    bumpPlayback,
    draftNonempty,
    fetchWithDeadline,
    parseError,
  } = app;
  async function playReply(messageId) {
    if (!state.sessionId) return;
    if (state.speechPending && state.speechPending.generation === state.generation) {
      if (state.speechPending.messageId === messageId) return;
      bumpPlayback();
    }
    const gen = state.generation;
    const token = bumpPlayback();
    state.speechPending = { generation: gen, messageId, token };
    app.updateSpeechButtons();
    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/speech`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "audio/wav, audio/mpeg",
          },
          body: JSON.stringify({ message_id: messageId }),
        },
        DEADLINE_TTS_MS
      );
      if (gen !== state.generation || token !== state.playbackToken) return;
      if (!response.ok) {
        const err = await parseError(response);
        showAlert(err.message);
        return;
      }
      const blob = await response.blob();
      if (gen !== state.generation || token !== state.playbackToken) return;
      if (!blob.size || (blob.type && !blob.type.startsWith("audio/"))) {
        showAlert(ui("ui.playback_fallback"));
        return;
      }
      const url = URL.createObjectURL(blob);
      if (gen !== state.generation || token !== state.playbackToken) {
        URL.revokeObjectURL(url);
        return;
      }
      revokeAudioUrl();
      if (gen !== state.generation || token !== state.playbackToken) {
        URL.revokeObjectURL(url);
        return;
      }
      state.objectUrl = url;
      els.audio.src = url;
      try {
        await els.audio.play();
      } catch (_) {
        if (gen === state.generation && token === state.playbackToken) {
          showAlert(ui("ui.playback_fallback"));
        }
      }
    } catch (e) {
      if (gen !== state.generation || token !== state.playbackToken) return;
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? ui("errors.provider_timeout") : ui("errors.provider_unavailable"));
    } finally {
      if (
        state.speechPending &&
        state.speechPending.generation === gen &&
        state.speechPending.messageId === messageId &&
        state.speechPending.token === token
      ) {
        state.speechPending = null;
        app.updateSpeechButtons();
      }
    }
  }

  function clearRecordingTimers(rec) {
    for (const timer of rec.timers) {
      clearTimeout(timer);
      clearInterval(timer);
    }
    rec.timers.clear();
  }

  function closeRecordingResources(rec) {
    rec.stopped = true;
    clearRecordingTimers(rec);
    try {
      rec.processor.disconnect();
      rec.source.disconnect();
    } catch (_) {
      /* ignore */
    }
    rec.stream.getTracks().forEach((track) => track.stop());
    try {
      rec.ctx.close();
    } catch (_) {
      /* ignore */
    }
    if (state.recording === rec) state.recording = null;
  }

  function discardRecording() {
    if (!state.recording) return;
    closeRecordingResources(state.recording);
    app.updateControls();
  }

  async function onTranscribe() {
    if (state.sttPending || !state.audioBlob || draftNonempty()) return;
    if (!transcriptionGate.tryAcquire()) return;
    const gen = state.generation;
    const controller = new AbortController();
    state.sttAbort = controller;
    state.sttPending = true;
    app.updateControls();
    clearAlert();
    try {
      const body = new FormData();
      body.append(
        "file",
        state.audioBlob,
        state.audioBlob.name || "recording.wav"
      );
      const response = await fetchWithDeadline(
        "/api/transcriptions",
        {
          method: "POST",
          body,
        },
        DEADLINE_STT_MS,
        controller
      );
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await parseError(response);
        if (gen === state.generation) {
          state.audioBlob = null;
          showAlert(err.message);
        }
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      const text = data && typeof data.text === "string" ? data.text : "";
      els.message.value = text;
      state.inputMode = "transcript";
      state.audioBlob = null;
      if (els.wav) els.wav.value = "";
      clearAlert();
    } catch (e) {
      if (gen !== state.generation) return;
      state.audioBlob = null;
      if (e && e.name === "AbortError") {
        showAlert(ui("errors.provider_timeout"));
        return;
      }
      showAlert(ui("errors.provider_unavailable"));
    } finally {
      transcriptionGate.release();
      if (gen === state.generation) {
        state.sttPending = false;
        state.sttAbort = null;
        app.updateControls();
      }
    }
  }

  async function startRecording() {
    if (
      state.recording ||
      state.recordingPending ||
      state.sttPending ||
      draftNonempty()
    ) return;
    clearAlert();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showAlert(ui("ui.mic_denied"));
      return;
    }
    const gen = state.generation;
    let stream = null;
    let ctx = null;
    state.recordingPending = true;
    app.updateControls();
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // New intake or page exit may have happened while permission was pending.
      if (gen !== state.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      ctx = new AudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      if (gen !== state.generation) {
        stream.getTracks().forEach((track) => track.stop());
        try { ctx.close(); } catch (_) { /* ignore */ }
        return;
      }
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      const accumulator = new window.OrbioRecording.RecordingAccumulator(ctx.sampleRate);
      const rec = {
        stream,
        ctx,
        processor,
        source,
        accumulator,
        chunks: accumulator.chunks,
        stopped: false,
        timers: new Set(),
      };

      processor.onaudioprocess = (event) => {
        if (rec.stopped) return;
        const input = event.inputBuffer.getChannelData(0);
        if (!accumulator.append(input)) stopRecording({ force: true });
      };
      source.connect(processor);
      processor.connect(ctx.destination);
      rec.timers.add(setTimeout(() => stopRecording({ force: true }), MAX_RECORD_SECONDS * 1000));
      state.recording = rec;
      els.stopRecording.hidden = false;
    } catch (_) {
      if (stream) stream.getTracks().forEach((track) => track.stop());
      if (ctx) {
        try { ctx.close(); } catch (_) { /* ignore */ }
      }
      if (gen === state.generation) showAlert(ui("ui.mic_denied"));
    } finally {
      if (gen === state.generation) {
        state.recordingPending = false;
        app.updateControls();
      }
    }
  }

  function stopRecording({ force = false } = {}) {
    const rec = state.recording;
    if (!rec || rec.stopped) return;
    const hasSignal = rec.chunks.some((chunk) => {
      for (let i = 0; i < chunk.length; i += 1) {
        if (Math.abs(chunk[i]) >= 128 / 32768) return true;
      }
      return false;
    });
    // Chromium's fake input starts with a short silent handshake. Keep the
    // live graph running briefly so a real take is not misclassified as silent.
    if (!force && !hasSignal && !rec.waitingForSignal) {
      rec.waitingForSignal = true;
      const timer = setTimeout(() => {
        rec.timers.delete(timer);
        stopRecording();
      }, 250);
      rec.timers.add(timer);
      return;
    }
    clearRecordingTimers(rec);
    rec.stopped = true;
    try {
      rec.processor.disconnect();
      rec.source.disconnect();
    } catch (_) {
      /* ignore */
    }
    rec.stream.getTracks().forEach((t) => t.stop());
    const sampleRate = rec.ctx.sampleRate;
    const merged = rec.accumulator.toSamples(Math.floor(sampleRate * 0.005));
    try {
      rec.ctx.close();
    } catch (_) {
      /* ignore */
    }
    state.recording = null;
    const blob = encodeWav(merged, sampleRate);
    if (blob.size > MAX_WAV_BYTES) {
      showAlert(ui("errors.invalid_audio"));
      state.audioBlob = null;
    } else {
      state.audioBlob = blob;
    }
    els.stopRecording.hidden = true;
    app.updateControls();
    if (state.audioBlob) void onTranscribe();
  }

  function onWavSelected() {
    const file = els.wav.files && els.wav.files[0];
    state.audioBlob = file || null;
    els.wav.value = "";
    app.updateControls();
    if (state.audioBlob) void onTranscribe();
  }

  Object.assign(app, {
    playReply,
    clearRecordingTimers,
    closeRecordingResources,
    discardRecording,
    onTranscribe,
    startRecording,
    stopRecording,
    onWavSelected,
  });
})();
