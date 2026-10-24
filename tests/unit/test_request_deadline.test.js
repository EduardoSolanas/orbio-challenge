const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { fetchWithDeadline } = require("../../app/static/request.js");

function stalledServer(body, delayMs) {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    setTimeout(() => res.end(body), delayMs);
  });
  return server;
}

async function withServer(server, fn) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await fn(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("JSON body stalls after headers and is aborted by the deadline", async () => {
  const server = stalledServer(JSON.stringify({ ok: true }), 100);
  await withServer(server, async (url) => {
    await assert.rejects(
      fetchWithDeadline(url, {}, 20),
      (error) => error && error.name === "AbortError"
    );
  });
});

test("audio body stalls after headers and is aborted by the deadline", async () => {
  const server = stalledServer("RIFFaudio", 100);
  await withServer(server, async (url) => {
    await assert.rejects(
      fetchWithDeadline(url, {}, 20),
      (error) => error && error.name === "AbortError"
    );
  });
});

test("a slow complete body within the deadline succeeds", async () => {
  const server = stalledServer(JSON.stringify({ ok: true }), 10);
  await withServer(server, async (url) => {
    const response = await fetchWithDeadline(url, {}, 100);
    assert.deepEqual(await response.json(), { ok: true });
  });
});
