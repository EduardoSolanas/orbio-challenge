(() => {
  "use strict";

  const STORAGE_KEY = "northstar.session_id";
  const MAX_CHARS = 4000;
  const MAX_WAV_BYTES = 10 * 1024 * 1024;
  const MAX_RECORD_SECONDS = 60;
  const DEADLINE_SESSION_MS = 5000;
  const DEADLINE_TURN_MS = 35000;
  const DEADLINE_STT_MS = 45000;
  const DEADLINE_TTS_MS = 30000;
  const PLAYBACK_FALLBACK =
    "Audio could not play; your text reply is still available.";
  const CHAR_LIMIT_ALERT =
    "Messages must contain at most 4000 characters.";
  const MIC_DENIED_ALERT =
    "Microphone access was denied or is unavailable. You can type or upload a WAV recording.";
  const TIMEOUT_ALERT = "The AI request timed out. Please retry.";
  const NETWORK_ALERT = "The AI service is unavailable. Please retry.";

  const els = {
    log: document.getElementById("transcript"),
    review: document.getElementById("review"),
    alert: document.getElementById("alert"),
    message: document.getElementById("message"),
    send: document.getElementById("send"),
    retry: document.getElementById("retry"),
    confirm: document.getElementById("confirm"),
    newIntake: document.getElementById("new-intake"),
    startRecording: document.getElementById("start-recording"),
    stopRecording: document.getElementById("stop-recording"),
    transcribe: document.getElementById("transcribe"),
    wav: document.getElementById("wav-recording"),
    audio: document.getElementById("reply-audio"),
    help: document.getElementById("transcribe-help"),
  };

  const state = {
    sessionId: null,
    revision: 0,
    status: "collecting",
    summary: null,
    pendingReviewRevision: null,
    pendingClarifications: {},
    messages: [],
    inputMode: "text",
    generation: 0,
    playbackToken: 0,
    objectUrl: null,
    pending: false,
    sttPending: false,
    createPending: false,
    retryKind: null, // "resume" | "turn" | "create" | null
    retryPayload: null,
    charLimitAlert: false,
    audioBlob: null,
    recording: null,
  };

  function isCanonicalUuid(value) {
    return (
      typeof value === "string" &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
        value
      )
    );
  }

  function newRequestId() {
    return crypto.randomUUID();
  }

  function setStoredId(id) {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  }

  function readStoredId() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null || raw === undefined) return null;
    if (!isCanonicalUuid(raw)) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return raw;
  }

  function textNode(value) {
    return document.createTextNode(value == null ? "" : String(value));
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function showAlert(message) {
    clearChildren(els.alert);
    els.alert.appendChild(textNode(message));
    els.alert.hidden = false;
  }

  function clearAlert() {
    state.charLimitAlert = false;
    clearChildren(els.alert);
    els.alert.hidden = true;
  }

  function setRetry(kind, payload) {
    state.retryKind = kind;
    state.retryPayload = payload || null;
    els.retry.hidden = !kind;
  }

  function clearRetry() {
    setRetry(null, null);
  }

  function revokeAudioUrl() {
    if (state.objectUrl) {
      try {
        els.audio.pause();
      } catch (_) {
        /* ignore */
      }
      URL.revokeObjectURL(state.objectUrl);
      state.objectUrl = null;
    }
    els.audio.removeAttribute("src");
    els.audio.load();
  }

  function bumpPlayback() {
    state.playbackToken += 1;
    return state.playbackToken;
  }

  function issuesPresent() {
    return Object.keys(state.pendingClarifications || {}).length > 0;
  }

  function canConfirm() {
    return (
      state.status === "awaiting_confirmation" &&
      state.pendingReviewRevision != null &&
      !issuesPresent() &&
      state.status !== "completed"
    );
  }

  function draftValue() {
    return els.message.value;
  }

  function draftNonempty() {
    return draftValue().length > 0;
  }

  function updateControls() {
    const completed = state.status === "completed";
    const mutating =
      state.pending || state.createPending || state.sttPending;
    const readonly =
      completed || state.pending || state.createPending || state.sttPending;

    els.message.readOnly = readonly;
    if (completed) {
      els.message.setAttribute("readonly", "readonly");
    }

    const overLimit = draftValue().length > MAX_CHARS;
    const sendOk =
      !completed &&
      !mutating &&
      !overLimit &&
      draftValue().trim().length > 0;

    els.send.disabled = !sendOk;
    els.confirm.hidden = !canConfirm() || completed;
    els.confirm.disabled =
      !canConfirm() || mutating || completed;

    const transcribeBlocked =
      draftNonempty() || mutating || completed || !state.audioBlob;
    els.transcribe.disabled =
      draftNonempty() ||
      state.pending ||
      state.createPending ||
      state.sttPending ||
      completed ||
      !state.audioBlob;

    els.help.hidden = !draftNonempty() || completed;

    els.newIntake.disabled = state.pending || state.createPending;
    // During STT, New intake remains available
    if (state.sttPending && !state.pending && !state.createPending) {
      els.newIntake.disabled = false;
    }

    els.startRecording.disabled =
      !!state.recording || mutating || completed;
    els.stopRecording.hidden = !state.recording;
    els.stopRecording.disabled = !state.recording;
    els.wav.disabled = mutating || completed;

    if (overLimit && !state.charLimitAlert) {
      state.charLimitAlert = true;
      showAlert(CHAR_LIMIT_ALERT);
    } else if (!overLimit && state.charLimitAlert) {
      clearAlert();
    }
  }

  function renderReview() {
    if (canConfirm() && state.summary) {
      clearChildren(els.review);
      els.review.appendChild(textNode(state.summary));
      els.review.hidden = false;
    } else {
      clearChildren(els.review);
      els.review.hidden = true;
    }
  }

  function renderSources(container, citations) {
    if (!citations || !citations.length) return;
    const region = document.createElement("div");
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "Sources");
    region.className = "sources";
    for (const cite of citations) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.appendChild(
        textNode(`${cite.title} ${cite.source_id}`)
      );
      details.appendChild(summary);
      const body = document.createElement("div");
      body.className = "sources__body";
      body.appendChild(textNode(cite.excerpt));
      details.appendChild(body);
      const version = document.createElement("p");
      version.className = "sources__version";
      version.appendChild(
        textNode(`Knowledge base version: ${cite.kb_version}`)
      );
      details.appendChild(version);
      region.appendChild(details);
    }
    container.appendChild(region);
  }

  function renderMessage(msg) {
    const wrap = document.createElement("article");
    wrap.className = `msg msg--${msg.role}`;
    const role = document.createElement("span");
    role.className = "msg__role";
    role.appendChild(textNode(msg.role === "user" ? "You" : "Assistant"));
    wrap.appendChild(role);
    const text = document.createElement("div");
    text.className = "msg__text";
    text.appendChild(textNode(msg.text));
    wrap.appendChild(text);
    if (msg.role === "assistant" && msg.id) {
      const playWrap = document.createElement("div");
      playWrap.className = "msg__play";
      const play = document.createElement("button");
      play.type = "button";
      play.className = "btn";
      play.appendChild(textNode(`Play reply ${msg.id}`));
      play.addEventListener("click", () => playReply(msg.id));
      playWrap.appendChild(play);
      wrap.appendChild(playWrap);
      renderSources(wrap, msg.citations || []);
    }
    return wrap;
  }

  function renderTranscript() {
    clearChildren(els.log);
    for (const msg of state.messages) {
      els.log.appendChild(renderMessage(msg));
    }
  }

  function applySessionView(view, { publishId = true } = {}) {
    if (publishId) {
      state.sessionId = view.session_id;
      setStoredId(view.session_id);
    }
    state.revision = view.revision;
    state.status = view.status;
    state.summary = view.summary;
    state.pendingReviewRevision = view.pending_review_revision;
    state.pendingClarifications = view.pending_clarifications || {};
    state.messages = (view.messages || []).map((m) => ({
      id: m.id,
      role: m.role,
      text: m.text,
      input_mode: m.input_mode,
      citations: m.citations || [],
    }));
    renderTranscript();
    renderReview();
    updateControls();
  }

  function applyTurnResponse(response, userText, inputMode) {
    state.revision = response.revision;
    state.status = response.status;
    state.summary = response.summary;
    state.pendingReviewRevision = response.pending_review_revision;
    state.pendingClarifications = response.pending_clarifications || {};
    if (userText != null) {
      state.messages.push({
        role: "user",
        text: userText,
        input_mode: inputMode,
        citations: [],
      });
    }
    state.messages.push({
      id: response.message_id,
      role: "assistant",
      text: response.reply,
      input_mode: "system",
      citations: response.citations || [],
    });
    renderTranscript();
    renderReview();
    updateControls();
  }

  async function fetchWithDeadline(url, options, deadlineMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), deadlineMs);
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
      });
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  async function parseError(response) {
    try {
      const data = await response.json();
      const message =
        (data && data.error && data.error.message) ||
        NETWORK_ALERT;
      const code = data && data.error && data.error.code;
      const retryable = !!(data && data.error && data.error.retryable);
      const currentRevision =
        data && data.error && data.error.current_revision;
      return { message, code, retryable, currentRevision, data };
    } catch (_) {
      return {
        message: NETWORK_ALERT,
        code: null,
        retryable: true,
        currentRevision: undefined,
        data: null,
      };
    }
  }

  async function createSession({ generation } = {}) {
    const gen = generation == null ? state.generation : generation;
    const response = await fetchWithDeadline(
      "/api/sessions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: "{}",
      },
      DEADLINE_SESSION_MS
    );
    if (gen !== state.generation) return { stale: true, response };
    if (!response.ok) {
      const err = await parseError(response);
      return { ok: false, err, response };
    }
    const view = await response.json();
    if (gen !== state.generation) return { stale: true, response };
    return { ok: true, view, response };
  }

  async function getSession(sessionId, { generation } = {}) {
    const gen = generation == null ? state.generation : generation;
    const response = await fetchWithDeadline(
      `/api/sessions/${sessionId}`,
      { method: "GET", headers: { Accept: "application/json" } },
      DEADLINE_SESSION_MS
    );
    if (gen !== state.generation) return { stale: true, response };
    return { response };
  }

  async function bootstrap() {
    clearAlert();
    clearRetry();
    const stored = readStoredId();
    if (!stored) {
      state.createPending = true;
      updateControls();
      try {
        const result = await createSession();
        if (result.stale) return;
        if (!result.ok) {
          showAlert(result.err.message);
          setRetry("create", null);
          state.createPending = false;
          updateControls();
          return;
        }
        applySessionView(result.view);
        clearAlert();
        clearRetry();
      } catch (e) {
        const aborted = e && e.name === "AbortError";
        showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
        setRetry("create", null);
      } finally {
        state.createPending = false;
        updateControls();
      }
      return;
    }

    try {
      const { response, stale } = await getSession(stored);
      if (stale) return;
      if (response.status === 404) {
        setStoredId(null);
        state.sessionId = null;
        await bootstrapCreateOnly();
        return;
      }
      if (!response.ok) {
        const err = await parseError(response);
        showAlert(err.message);
        setRetry("resume", { sessionId: stored });
        state.sessionId = stored;
        updateControls();
        return;
      }
      const view = await response.json();
      applySessionView(view);
      clearAlert();
      clearRetry();
    } catch (e) {
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
      setRetry("resume", { sessionId: stored });
      state.sessionId = stored;
      updateControls();
    }
  }

  async function bootstrapCreateOnly() {
    state.createPending = true;
    updateControls();
    try {
      const result = await createSession();
      if (result.stale) return;
      if (!result.ok) {
        showAlert(result.err.message);
        setRetry("create", null);
        return;
      }
      applySessionView(result.view);
      clearAlert();
      clearRetry();
    } catch (e) {
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
      setRetry("create", null);
    } finally {
      state.createPending = false;
      updateControls();
    }
  }

  async function onRetry() {
    if (!state.retryKind) return;
    if (state.retryKind === "resume") {
      const sid = (state.retryPayload && state.retryPayload.sessionId) || readStoredId();
      if (!sid) return;
      clearAlert();
      try {
        const { response, stale } = await getSession(sid);
        if (stale) return;
        if (!response.ok) {
          const err = await parseError(response);
          showAlert(err.message);
          setRetry("resume", { sessionId: sid });
          return;
        }
        const view = await response.json();
        applySessionView(view);
        clearAlert();
        clearRetry();
      } catch (e) {
        const aborted = e && e.name === "AbortError";
        showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
        setRetry("resume", { sessionId: sid });
      }
      return;
    }
    if (state.retryKind === "create") {
      await startNewIntake({ retryOnly: true });
      return;
    }
    if (state.retryKind === "turn" && state.retryPayload) {
      await submitTurnPayload(state.retryPayload, { isRetry: true });
    }
  }

  async function startNewIntake({ retryOnly = false } = {}) {
    state.generation += 1;
    const gen = state.generation;
    bumpPlayback();
    revokeAudioUrl();
    if (state.sttAbort) {
      try {
        state.sttAbort.abort();
      } catch (_) {
        /* ignore */
      }
      state.sttAbort = null;
    }
    state.sttPending = false;
    state.audioBlob = null;
    state.inputMode = "text";
    els.message.value = "";
    if (els.wav) els.wav.value = "";
    clearAlert();
    clearRetry();

    // Keep prior transcript visible read-only until new session publishes
    state.createPending = true;
    state.pending = false;
    updateControls();

    try {
      const result = await createSession({ generation: gen });
      if (gen !== state.generation || result.stale) return;
      if (!result.ok) {
        showAlert(result.err.message);
        setRetry("create", null);
        // retain prior id/transcript
        updateControls();
        return;
      }
      applySessionView(result.view);
      clearAlert();
      clearRetry();
    } catch (e) {
      if (gen !== state.generation) return;
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
      setRetry("create", null);
    } finally {
      if (gen === state.generation) {
        state.createPending = false;
        updateControls();
      }
    }
  }

  async function submitTurnPayload(
    payload,
    { isRetry = false, alreadyPending = false } = {}
  ) {
    if (!state.sessionId) return;
    if (!alreadyPending) {
      if (state.pending) return;
      state.pending = true;
      updateControls();
    }
    const gen = state.generation;
    clearAlert();

    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/turns`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(payload),
        },
        DEADLINE_TURN_MS
      );
      if (gen !== state.generation) return;

      if (response.status === 409) {
        const err = await parseError(response);
        if (err.code === "revision_conflict") {
          // Refresh state; retain unsent text; new request_id required next time
          els.message.value = payload.text;
          state.inputMode = payload.input_mode;
          try {
            const refreshed = await getSession(state.sessionId, {
              generation: gen,
            });
            if (gen !== state.generation || refreshed.stale) return;
            if (refreshed.response.ok) {
              const view = await refreshed.response.json();
              if (gen !== state.generation) return;
              applySessionView(view);
            }
          } catch (_) {
            /* keep local */
          }
          showAlert(err.message);
          clearRetry();
          state.retryPayload = null;
          return;
        }
        showAlert(err.message);
        if (err.retryable) setRetry("turn", payload);
        else clearRetry();
        els.message.value = payload.text;
        return;
      }

      if (!response.ok) {
        const err = await parseError(response);
        showAlert(err.message);
        els.message.value = payload.text;
        state.inputMode = payload.input_mode;
        if (err.retryable || response.status >= 500) {
          setRetry("turn", payload);
        } else {
          clearRetry();
        }
        return;
      }

      const data = await response.json();
      if (gen !== state.generation) return;
      applyTurnResponse(data, payload.text, payload.input_mode);
      els.message.value = "";
      state.inputMode = "text";
      state.audioBlob = null;
      if (els.wav) els.wav.value = "";
      clearAlert();
      clearRetry();
    } catch (e) {
      if (gen !== state.generation) return;
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
      els.message.value = payload.text;
      state.inputMode = payload.input_mode;
      setRetry("turn", payload);
    } finally {
      if (gen === state.generation) {
        state.pending = false;
        updateControls();
      }
    }
  }

  async function onSend() {
    if (els.send.disabled || state.pending || state.createPending || state.sttPending) {
      return;
    }
    const text = draftValue();
    if (!text.trim() || text.length > MAX_CHARS) return;
    clearRetry();
    const payload = {
      request_id: newRequestId(),
      expected_revision: state.revision,
      text,
      input_mode: state.inputMode === "transcript" ? "transcript" : "text",
    };
    state.retryPayload = payload;
    state.pending = true;
    updateControls();
    await submitTurnPayload(payload, { alreadyPending: true });
  }

  async function onConfirm() {
    if (
      els.confirm.disabled ||
      els.confirm.hidden ||
      state.pending ||
      state.createPending
    ) {
      return;
    }
    if (!canConfirm() || !state.sessionId) return;
    const gen = state.generation;
    const payload = {
      request_id: newRequestId(),
      expected_revision: state.revision,
      review_revision: state.pendingReviewRevision,
    };
    state.pending = true;
    updateControls();
    clearAlert();
    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/confirm`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(payload),
        },
        DEADLINE_TURN_MS
      );
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await parseError(response);
        showAlert(err.message);
        if (err.retryable) setRetry("turn", null);
        return;
      }
      const data = await response.json();
      if (gen !== state.generation) return;
      applyTurnResponse(data, null, null);
      // Re-sync from GET so completed messages/receipt match durable state.
      try {
        const refreshed = await getSession(state.sessionId, { generation: gen });
        if (
          gen === state.generation &&
          !refreshed.stale &&
          refreshed.response.ok
        ) {
          const view = await refreshed.response.json();
          if (gen === state.generation) applySessionView(view);
        }
      } catch (_) {
        /* turn response already applied */
      }
      clearAlert();
      clearRetry();
    } catch (e) {
      if (gen !== state.generation) return;
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
    } finally {
      if (gen === state.generation) {
        state.pending = false;
        updateControls();
      }
    }
  }

  async function playReply(messageId) {
    if (!state.sessionId) return;
    const gen = state.generation;
    const token = bumpPlayback();
    // Abort prior in-flight TTS by token only (no shared abort required)
    try {
      const response = await fetchWithDeadline(
        `/api/sessions/${state.sessionId}/speech`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "audio/mpeg",
          },
          body: JSON.stringify({ message_id: messageId }),
        },
        DEADLINE_TTS_MS
      );
      if (gen !== state.generation || token !== state.playbackToken) {
        return;
      }
      if (!response.ok) {
        const err = await parseError(response);
        if (gen === state.generation && token === state.playbackToken) {
          showAlert(err.message);
        }
        return;
      }
      const blob = await response.blob();
      if (gen !== state.generation || token !== state.playbackToken) {
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
          showAlert(PLAYBACK_FALLBACK);
        }
      }
    } catch (e) {
      if (gen !== state.generation || token !== state.playbackToken) return;
      const aborted = e && e.name === "AbortError";
      showAlert(aborted ? TIMEOUT_ALERT : NETWORK_ALERT);
    }
  }

  async function onTranscribe() {
    if (els.transcribe.disabled || !state.audioBlob) return;
    if (draftNonempty()) return;
    const gen = state.generation;
    const controller = new AbortController();
    state.sttAbort = controller;
    state.sttPending = true;
    updateControls();
    clearAlert();
    const timer = setTimeout(() => controller.abort(), DEADLINE_STT_MS);
    try {
      const body = new FormData();
      body.append(
        "file",
        state.audioBlob,
        state.audioBlob.name || "recording.wav"
      );
      const response = await fetch("/api/transcriptions", {
        method: "POST",
        body,
        signal: controller.signal,
      });
      if (gen !== state.generation) return;
      if (!response.ok) {
        const err = await parseError(response);
        if (gen === state.generation) showAlert(err.message);
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
      if (e && e.name === "AbortError") {
        showAlert(TIMEOUT_ALERT);
        return;
      }
      showAlert(NETWORK_ALERT);
    } finally {
      clearTimeout(timer);
      if (gen === state.generation) {
        state.sttPending = false;
        state.sttAbort = null;
        updateControls();
      }
    }
  }

  function encodeWav(float32Samples, sampleRate) {
    const numFrames = float32Samples.length;
    const buffer = new ArrayBuffer(44 + numFrames * 2);
    const view = new DataView(buffer);
    const writeStr = (offset, str) => {
      for (let i = 0; i < str.length; i += 1) {
        view.setUint8(offset + i, str.charCodeAt(i));
      }
    };
    writeStr(0, "RIFF");
    view.setUint32(4, 36 + numFrames * 2, true);
    writeStr(8, "WAVE");
    writeStr(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, "data");
    view.setUint32(40, numFrames * 2, true);
    let offset = 44;
    for (let i = 0; i < numFrames; i += 1) {
      let s = Math.max(-1, Math.min(1, float32Samples[i]));
      view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      offset += 2;
    }
    return new Blob([buffer], { type: "audio/wav" });
  }

  async function startRecording() {
    if (state.recording) return;
    clearAlert();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showAlert(MIC_DENIED_ALERT);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (_) {
      showAlert(MIC_DENIED_ALERT);
      return;
    }
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx();
    if (ctx.state === "suspended") await ctx.resume();
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    let stopped = false;

    processor.onaudioprocess = (event) => {
      if (stopped) return;
      const input = event.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(input));
      const total = chunks.reduce((n, c) => n + c.length, 0);
      if (total / ctx.sampleRate >= MAX_RECORD_SECONDS) {
        stopRecording();
      }
    };
    source.connect(processor);
    processor.connect(ctx.destination);

    state.recording = { stream, ctx, processor, source, chunks, stopped: false };
    els.stopRecording.hidden = false;
    updateControls();
  }

  function stopRecording() {
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
    if (!hasSignal && !rec.waitingForSignal) {
      rec.waitingForSignal = true;
      setTimeout(stopRecording, 250);
      return;
    }
    rec.stopped = true;
    try {
      rec.processor.disconnect();
      rec.source.disconnect();
    } catch (_) {
      /* ignore */
    }
    rec.stream.getTracks().forEach((t) => t.stop());
    const total = rec.chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Float32Array(total);
    let offset = 0;
    for (const c of rec.chunks) {
      merged.set(c, offset);
      offset += c.length;
    }
    const sampleRate = rec.ctx.sampleRate;
    try {
      rec.ctx.close();
    } catch (_) {
      /* ignore */
    }
    state.recording = null;
    const blob = encodeWav(merged, sampleRate);
    if (blob.size > MAX_WAV_BYTES) {
      showAlert("The WAV recording is invalid or exceeds the audio limits.");
      state.audioBlob = null;
    } else {
      state.audioBlob = blob;
    }
    els.stopRecording.hidden = true;
    updateControls();
  }

  function onWavSelected() {
    const file = els.wav.files && els.wav.files[0];
    state.audioBlob = file || null;
    updateControls();
  }

  function onMessageInput() {
    // Editing a failed draft cancels retry payload
    if (state.retryKind === "turn") {
      clearRetry();
    }
    // Fresh typing after send uses text mode unless still in transcript draft
    if (state.inputMode === "transcript" && !draftNonempty()) {
      state.inputMode = "text";
    }
    updateControls();
  }

  function onPageHide() {
    try {
      els.audio.pause();
    } catch (_) {
      /* ignore */
    }
    revokeAudioUrl();
  }

  els.send.addEventListener("click", () => {
    onSend();
  });
  els.retry.addEventListener("click", () => {
    onRetry();
  });
  els.confirm.addEventListener("click", () => {
    onConfirm();
  });
  els.newIntake.addEventListener("click", () => {
    startNewIntake();
  });
  els.transcribe.addEventListener("click", () => {
    onTranscribe();
  });
  els.startRecording.addEventListener("click", () => {
    startRecording();
  });
  els.stopRecording.addEventListener("click", () => {
    stopRecording();
  });
  els.wav.addEventListener("change", onWavSelected);
  els.message.addEventListener("input", onMessageInput);
  window.addEventListener("pagehide", onPageHide);

  updateControls();
  bootstrap();
})();
