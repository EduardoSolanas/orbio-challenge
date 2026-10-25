/** Plays one spoken reply at a time and releases its object URL. */
export class AudioPlayer {
  constructor({ element, state, urls = globalThis.URL }) {
    this.element = element;
    this.state = state;
    this.urls = urls;
  }

  createUrl(blob) {
    return this.urls.createObjectURL(blob);
  }

  revokeUrl(url) {
    this.urls.revokeObjectURL(url);
  }

  pause() {
    try {
      this.element.pause();
    } catch (_) {
      /* ignore */
    }
  }

  /** Stop playback and free the current URL, if any. */
  release() {
    if (this.state.objectUrl) {
      this.pause();
      this.revokeUrl(this.state.objectUrl);
      this.state.objectUrl = null;
    }
    this.element.removeAttribute("src");
    this.element.load();
  }

  /** Make ``url`` the current source; it is released with the next ``release()``. */
  use(url) {
    this.state.objectUrl = url;
    this.element.src = url;
  }

  play() {
    return this.element.play();
  }
}
