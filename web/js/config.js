export const STORAGE_KEY = "northstar.session_id";
export const MAX_CHARS = 4000;
export const MAX_WAV_BYTES = 10 * 1024 * 1024;
export const MAX_RECORD_SECONDS = 60;

/** Per-request deadlines in milliseconds. */
export const DEADLINES = Object.freeze({
  session: 5000,
  turn: 35000,
  stt: 45000,
  tts: 30000,
});
