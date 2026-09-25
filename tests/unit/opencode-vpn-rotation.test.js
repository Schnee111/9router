import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The OpenCode 429 -> ProtonVPN IP rotation path.
 *
 * This behaviour lives in the `isOpencode` branch of proxyAwareFetch: a quota
 * 429 (FreeUsageLimitError / "Rate limit") triggers the VPN rotator on
 * port 28880 and retries through the proxy stack, up to 3 times. Before these
 * tests existed that branch was restructured with no coverage at all, and the
 * structural change mattered: the loop used to fall through to a final call
 * made WITHOUT the forced vpnOptions, which was another direct-egress path.
 *
 * Every assertion here also pins egress safety: an OpenCode call that reaches
 * fetch without a `dispatcher` is a direct (datacenter) connection.
 */

const OPENCODE_HOST = "opencode.ai";
const ROTATE_URL = "http://172.19.0.1:28880/rotate";

const QUOTA_429 = JSON.stringify({
  error: { message: "FreeUsageLimitError: quota exhausted" },
});

function opencodeCalls(fetchMock) {
  return fetchMock.mock.calls.filter((c) => String(c[0]).includes(OPENCODE_HOST));
}

function rotateCalls(fetchMock) {
  return fetchMock.mock.calls.filter((c) => String(c[0]) === ROTATE_URL);
}

/** Direct egress guard: every OpenCode call must carry a proxy dispatcher. */
function directCalls(fetchMock) {
  return opencodeCalls(fetchMock).filter((c) => !c[1]?.dispatcher);
}

async function loadProxyFetch(fetchMock) {
  vi.resetModules();
  vi.stubEnv("OPENCODE_PROXY_TRANSPORT_RETRIES", "2");
  vi.stubEnv("OPENCODE_PROXY_TRANSPORT_BACKOFF_MS", "1");
  vi.stubGlobal("fetch", fetchMock);
  return await import("../../open-sse/utils/proxyFetch.js");
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("OpenCode 429 VPN rotation", () => {
  it("rotates the VPN exit IP on a quota 429, then succeeds through the proxy", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    let opencodeHits = 0;
    const fetchMock = vi.fn(async (input, init) => {
      if (String(input).includes(OPENCODE_HOST)) {
        opencodeHits += 1;
        if (opencodeHits === 1) {
          return new Response(QUOTA_429, { status: 429 });
        }
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response("", { status: 200 });
    });

    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);
    const resp = await proxyAwareFetch("https://opencode.ai/zen/v1/chat/completions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });

    expect(resp.status).toBe(200);
    expect(opencodeHits).toBe(2);
    expect(rotateCalls(fetchMock)).toHaveLength(1);
    expect(directCalls(fetchMock)).toHaveLength(0);
  });

  it("returns the upstream 429 when the rotate trigger itself fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchMock = vi.fn(async (input) => {
      if (String(input).includes(OPENCODE_HOST)) {
        return new Response(QUOTA_429, { status: 429 });
      }
      throw new Error("ECONNREFUSED 172.19.0.1:28880");
    });

    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);
    const resp = await proxyAwareFetch("https://opencode.ai/zen/v1/chat/completions", {
      method: "POST",
      body: "{}",
    });

    expect(resp.status).toBe(429);
    // One upstream attempt only: a dead rotator must not spawn extra calls,
    // and certainly not a direct one.
    expect(opencodeCalls(fetchMock)).toHaveLength(1);
    expect(directCalls(fetchMock)).toHaveLength(0);
  });

  it("does not rotate on a 429 that is not a quota error", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchMock = vi.fn(async (input) => {
      if (String(input).includes(OPENCODE_HOST)) {
        return new Response('{"error":"too many concurrent connections"}', { status: 429 });
      }
      return new Response("", { status: 200 });
    });

    const { proxyAwareFetch } = await loadProxyFetch(fetchMock);
    const resp = await proxyAwareFetch("https://opencode.ai/zen/v1/chat/completions", {
      method: "POST",
      body: "{}",
    });

    expect(resp.status).toBe(429);
    expect(rotateCalls(fetchMock)).toHaveLength(0);
    expect(opencodeCalls(fetchMock)).toHaveLength(1);
    expect(directCalls(fetchMock)).toHaveLength(0);
  });

  it(
    "exhausts the rotation budget by returning the last response, never by leaving the VPN path",
    async () => {
      vi.spyOn(console, "warn").mockImplementation(() => {});
      const fetchMock = vi.fn(async (input) => {
        if (String(input).includes(OPENCODE_HOST)) {
          return new Response(QUOTA_429, { status: 429 });
        }
        return new Response("", { status: 200 });
      });

      const { proxyAwareFetch } = await loadProxyFetch(fetchMock);
      const resp = await proxyAwareFetch("https://opencode.ai/zen/v1/chat/completions", {
        method: "POST",
        body: "{}",
      });

      expect(resp.status).toBe(429);
      // Egress safety first: the pre-fix fall-through made a 4th call outside
      // the forced vpnOptions, i.e. a direct datacenter connection.
      expect(directCalls(fetchMock)).toHaveLength(0);
      expect(opencodeCalls(fetchMock)).toHaveLength(3);
      expect(rotateCalls(fetchMock)).toHaveLength(3);
    },
    // 3 rotations x 1200ms settle sleep.
    15000
  );
});
