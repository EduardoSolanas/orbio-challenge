import { STORAGE_KEY } from "./config.js";

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Remembers the current session id in the browser between page loads. */
export class SessionStore {
  constructor(storage, key = STORAGE_KEY) {
    this.storage = storage;
    this.key = key;
  }

  static isCanonicalUuid(value) {
    return typeof value === "string" && CANONICAL_UUID.test(value);
  }

  save(id) {
    if (id) this.storage.setItem(this.key, id);
    else this.storage.removeItem(this.key);
  }

  /** The stored id, or null; a malformed value is discarded. */
  load() {
    const raw = this.storage.getItem(this.key);
    if (raw === null || raw === undefined) return null;
    if (!SessionStore.isCanonicalUuid(raw)) {
      this.storage.removeItem(this.key);
      return null;
    }
    return raw;
  }
}
