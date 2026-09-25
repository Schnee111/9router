import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * OpenCode egress must never fall back to a direct (datacenter) connection.
 *
 * These tests pin the two behaviours introduced for that guarantee:
 *   1. transport failures are retried within a bounded budget, then THROWN;
 *   2. non-OpenCode proxied hosts keep their legacy fail-open behaviour.
 *
 * `originalFetch` is captured at module load, so the mock must be installed
 * before the dynamic import, after vi.resetModules().
 */

const PROXY_ENV_KEYS = [
  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
  "http_proxy", "https_proxy", "all_proxy", "no_proxy",
];

const OPENCODE_URL = "https://opencode.ai/zen/v1/chat/completions";
const GENERIC_URL = "https://example.com/v1/models";
const TRANSPORT_RETRIES = "2";

async function loadProxyFetch(fetchMock) {
  vi.resetModules();
  for (const key of PROXY_ENV_KEYS) vi.stubEnv(key, "");
  // Keep the test fast; the default backoff is 300ms * attempt.
  vi.stubEnv("OPENCODE_PROXY_TRANSPORT_BACKOFF_MS", "1");
  vi.stubEnv("OPENCODE_PROXY_TRANSPORT_RETRIES", TRANSPORT_RETRIES);
  vi.stubGlobal("fetch", fetchMock);
  const mod = await import("../../open-sse/utils/proxyFetch.js");
  return mod;
}

const viaProxy = (call) => Boolean(call[1]?.dispatcher);
const transportError = (code) => {
  const err = new TypeError("fetch failed");
  err.cause = { code, message: code };
  return err;
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("OpenCode proxy fail-closed egress", () => {
  it("throws after the retry budget instead of falling back to direct", async () => {
    const fetchMock = vi.fn(async () => {
      throw transportError("ECONNREFUSED");
    });
    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);

    await expect(
      proxyAwareFetch(OPENCODE_URL, { method: "POST", body: "{\"model\":\"m\"}" })
    ).rejects.toThrow();

    // 1 initial attempt + 2 retries
    expect(fetchMock).toHaveBeenCalledTimes(1 + Number(TRANSPORT_RETRIES));
    // Every attempt carried a dispatcher — a direct call would have none.
    expect(fetchMock.mock.calls.every(viaProxy)).toBe(true);
  });

  it("recovers when the proxy comes back within the retry budget", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => {
        throw transportError("socket hang up");
      })
      .mockImplementationOnce(async () => new Response("ok", { status: 200 }));
    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);

    const resp = await proxyAwareFetch(OPENCODE_URL, { method: "POST", body: "{}" });

    expect(resp.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.every(viaProxy)).toBe(true);
  });

  it("does not retry a caller-initiated abort", async () => {
    const abort = new Error("The operation was aborted");
    abort.name = "AbortError";
    const fetchMock = vi.fn(async () => {
      throw abort;
    });
    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);

    await expect(
      proxyAwareFetch(OPENCODE_URL, { method: "POST", body: "{}" })
    ).rejects.toThrow(/aborted/i);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry a non-replayable body", async () => {
    const fetchMock = vi.fn(async () => {
      throw transportError("ECONNRESET");
    });
    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);

    await expect(
      proxyAwareFetch(OPENCODE_URL, {
        method: "POST",
        body: new ReadableStream({ start(c) { c.enqueue(new Uint8Array([1])); c.close(); } }),
      })
    ).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("non-OpenCode proxied hosts keep legacy behaviour", () => {
  it("still falls back to direct when the proxy fails", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => {
        throw transportError("ECONNREFUSED");
      })
      .mockImplementationOnce(async () => new Response("direct", { status: 200 }));
    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);

    const resp = await proxyAwareFetch(GENERIC_URL, { method: "GET" }, {
      connectionProxyEnabled: true,
      connectionProxyUrl: "http://proxy.test:8080",
    });

    expect(resp.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(viaProxy(fetchMock.mock.calls[0])).toBe(true);
    // Second attempt is the direct fallback — no dispatcher.
    expect(viaProxy(fetchMock.mock.calls[1])).toBe(false);
  });
});
