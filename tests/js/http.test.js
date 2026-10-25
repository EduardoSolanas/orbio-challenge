/** @jest-environment node */
import http from "node:http";
import { describe, expect, test } from "@jest/globals";

import { DeadlineHttp } from "../../app/static/js/http.js";

function stalledServer(body, delayMs) {
  return http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    setTimeout(() => res.end(body), delayMs);
  });
}

async function withServer(server, fn) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await fn(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

describe("DeadlineHttp.fetchWithDeadline", () => {
  const client = new DeadlineHttp();

  test("a JSON body that stalls after the headers is aborted by the deadline", async () => {
    await withServer(stalledServer(JSON.stringify({ ok: true }), 200), async (url) => {
      await expect(client.fetchWithDeadline(url, {}, 20)).rejects.toMatchObject({ name: "AbortError" });
    });
  });

  test("an audio body that stalls after the headers is aborted by the deadline", async () => {
    await withServer(stalledServer("RIFFaudio", 200), async (url) => {
      await expect(client.fetchWithDeadline(url, {}, 20)).rejects.toMatchObject({ name: "AbortError" });
    });
  });

  test("a slow complete body within the deadline succeeds", async () => {
    await withServer(stalledServer(JSON.stringify({ ok: true }), 10), async (url) => {
      const response = await client.fetchWithDeadline(url, {}, 1000);

      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ ok: true });
    });
  });

  test("the caller's abort controller can cancel the request early", async () => {
    await withServer(stalledServer("{}", 500), async (url) => {
      const controller = new AbortController();
      const pending = client.fetchWithDeadline(url, {}, 5000, controller);
      setTimeout(() => controller.abort(), 10);

      await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    });
  });

  test("passes the request options and the abort signal to fetch", async () => {
    const seen = [];
    const fake = new DeadlineHttp({
      fetchFn: async (url, options) => {
        seen.push({ url, options });
        return new Response("ok", { status: 201, statusText: "Created" });
      },
    });

    const response = await fake.fetchWithDeadline("/x", { method: "POST", body: "b" }, 1000);

    expect(seen[0].url).toBe("/x");
    expect(seen[0].options.method).toBe("POST");
    expect(seen[0].options.signal).toBeInstanceOf(AbortSignal);
    expect(response.status).toBe(201);
    expect(await response.text()).toBe("ok");
  });
});
