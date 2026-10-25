/** fetch with a hard deadline that also covers reading the response body. */
export class DeadlineHttp {
  constructor({
    fetchFn = (...args) => globalThis.fetch(...args),
    ResponseCtor = globalThis.Response,
    AbortControllerCtor = globalThis.AbortController,
  } = {}) {
    this.fetchFn = fetchFn;
    this.ResponseCtor = ResponseCtor;
    this.AbortControllerCtor = AbortControllerCtor;
  }

  async fetchWithDeadline(url, options, deadlineMs, abortController = new this.AbortControllerCtor()) {
    let finished = false;
    const timer = setTimeout(() => {
      if (!finished) abortController.abort();
    }, deadlineMs);

    try {
      const response = await this.fetchFn(url, { ...options, signal: abortController.signal });
      const body = await response.arrayBuffer();
      return new this.ResponseCtor(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    } finally {
      finished = true;
      clearTimeout(timer);
    }
  }
}
