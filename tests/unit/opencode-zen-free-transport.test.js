// The free gate on the keyed OpenCode lane is decided by the TLS ClientHello:
// the identical request that 429s (FreeUsageLimitError) on a native node fetch
// succeeds over the impersonating client the oc/ lane already uses. These tests
// pin that free-tier models on opencode-zen go through opencodeTlsFetch while
// paid models keep the plain path that carries the connection key.
import { describe, it, expect, vi, beforeEach } from "vitest";

const { executeMock, tlsFetchMock } = vi.hoisted(() => ({
  executeMock: vi.fn(),
  tlsFetchMock: vi.fn(),
}));

vi.mock("../../open-sse/executors/default.js", () => ({
  DefaultExecutor: class {
    constructor() {
      this.provider = "opencode-zen";
    }
    async execute(args) {
      return executeMock(args);
    }
    prepareRequestCredentials(args) {
      return { ...(args?.credentials || {}), _prepared: true };
    }
    buildHeaders() {
      return {};
    }
    transformRequest(_m, body) {
      return body;
    }
  },
}));

vi.mock("../../open-sse/services/opencodeTlsClient.js", () => ({
  opencodeTlsFetch: tlsFetchMock,
}));

vi.mock("../../open-sse/utils/sessionManager.js", () => ({
  resolveSessionId: vi.fn(() => "ses_000000000000AAAAAAAAAAAAAA"),
}));

let OpenCodeZenExecutor;

beforeEach(async () => {
  vi.resetModules();
  executeMock.mockReset().mockResolvedValue({ ok: true });
  // The executor module imports the default executor under the class it extends,
  // so re-import after the mock is in place.
  ({ OpenCodeZenExecutor } = await import("../../open-sse/executors/opencode-zen.js"));
});

describe("opencode-zen free-tier transport", () => {
  it("routes free models through the impersonating fetch", async () => {
    const ex = new OpenCodeZenExecutor();
    await ex.execute({ model: "deepseek-v4.1-flash-free", body: {}, credentials: {} });
    const args = executeMock.mock.calls[0][0];
    expect(typeof args.directFetch).toBe("function");
    expect(args.directFetch.proxyCapable).toBe(true);
  });

  it("passes the proxied adapter through to wreq-js", async () => {
    const ex = new OpenCodeZenExecutor();
    await ex.execute({ model: "deepseek-v4.1-flash-free", body: {}, credentials: {} });
    const { directFetch } = executeMock.mock.calls[0][0];
    tlsFetchMock.mockResolvedValue(new Response("ok", { status: 200 }));
    await directFetch("https://opencode.ai/inference/v1/chat/completions",
      { method: "POST", headers: { "x-opencode-client": "cli" }, body: "{}" },
      "http://203.0.113.9:3129");
    expect(tlsFetchMock).toHaveBeenCalledOnce();
    expect(tlsFetchMock.mock.calls[0][2].proxyUrl).toBe("http://203.0.113.9:3129");
  });

  it("keeps paid models on the plain path", async () => {
    const ex = new OpenCodeZenExecutor();
    await ex.execute({ model: "claude-fable-5", body: {}, credentials: {} });
    const args = executeMock.mock.calls[0][0];
    expect(args.directFetch).toBeUndefined();
    expect(args.credentials._prepared).toBe(true);
  });
});
