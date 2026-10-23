async function fetchWithDeadline(
  url,
  options,
  deadlineMs,
  abortController = new AbortController()
) {
  let finished = false;
  const timer = setTimeout(() => {
    if (!finished) abortController.abort();
  }, deadlineMs);

  try {
    const response = await fetch(url, {
      ...options,
      signal: abortController.signal,
    });
    const body = await response.arrayBuffer();
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } finally {
    finished = true;
    clearTimeout(timer);
  }
}

if (typeof window !== "undefined") {
  window.NorthstarRequest = { fetchWithDeadline };
}

if (typeof module !== "undefined") {
  module.exports = { fetchWithDeadline };
}
