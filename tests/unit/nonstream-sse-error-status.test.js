// A non-streaming request whose provider replies over SSE with an error chunk must
// return a non-2xx result. Before the fix the SSE branch of handleNonStreamingResponse
// only checked for an unparseable body, so `{ error }` flowed on as a successful 200
// body with no usage; Claude clients then crashed reading `usage.input_tokens`, and the
// account loop never fell back to the next account.
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/usageDb.js", () => ({
  appendRequestLog: vi.fn(async () => {}),
  saveRequestDetail: vi.fn(async () => {}),
  saveRequestUsage: vi.fn(async () => {}),
}));

const { FORMATS } = await import("../../open-sse/translator/formats.js");
const { handleNonStreamingResponse } = await import("../../open-sse/handlers/chatCore/nonStreamingHandler.js");

// Same wire shape as encodeSSEError() in open-sse/executors/kiro.js.
function sseError(error) {
  return `data: ${JSON.stringify({ error })}\n\ndata: [DONE]\n\n`;
}

function sseResponse(text) {
  return new Response(text, { status: 200, headers: { "content-type": "text/event-stream" } });
}

const okSSE = [
  { id: "chatcmpl-k1", model: "claude-opus-5.5", choices: [{ index: 0, delta: { role: "assistant", content: "Tokyo" } }] },
  { id: "chatcmpl-k1", choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 } },
].map((c) => `data: ${JSON.stringify(c)}\n\n`).join("") + "data: [DONE]\n\n";

function callHandler(providerResponse, { sourceFormat = FORMATS.CLAUDE, appendLog = () => {} } = {}) {
  return handleNonStreamingResponse({
    providerResponse,
    provider: "kiro",
    model: "claude-opus-5.5",
    sourceFormat,
    targetFormat: FORMATS.KIRO,
    body: { stream: false },
    stream: false,
    translatedBody: null,
    finalBody: null,
    requestStartTime: Date.now(),
    connectionId: "c1",
    apiKey: "test-key",
    clientRawRequest: null,
    onRequestSuccess: null,
    reqLogger: { logProviderResponse() {}, logConvertedResponse() {} },
    toolNameMap: null,
    customToolNames: null,
    trackDone: () => {},
    appendLog,
    pxpipe: null,
    reqTag: "t",
    log: null,
  });
}

describe("handleNonStreamingResponse: SSE error chunk", () => {
  it("Kiro error chunk without status → 502, not a 200 success", async () => {
    const result = await callHandler(sseResponse(sseError({
      message: "Kiro stream ended with a terminal failure",
      type: "upstream_error",
      code: "kiro_upstream_eventstream_error",
    })));
    expect(result.success).toBe(false);
    expect(result.status).toBe(502);
    expect(result.response.status).toBe(502);
    expect(result.error).toBe("Kiro stream ended with a terminal failure");
  });

  it("error chunk carrying a 4xx/5xx status keeps that status (account loop can lock/fall back)", async () => {
    const result = await callHandler(sseResponse(sseError({ message: "rate limited", status: 429 })));
    expect(result.success).toBe(false);
    expect(result.status).toBe(429);
  });

  it("error chunk with an out-of-range status maps to 502", async () => {
    const result = await callHandler(sseResponse(sseError({ message: "weird", status: 200 })));
    expect(result.success).toBe(false);
    expect(result.status).toBe(502);
  });

  it("error chunk without a message still gets a message", async () => {
    const result = await callHandler(sseResponse(sseError({ type: "upstream_error" })));
    expect(result.status).toBe(502);
    expect(result.error).toBe("Upstream SSE stream failed");
  });

  it("logs the failure instead of a 200 OK", async () => {
    const appendLog = vi.fn();
    await callHandler(sseResponse(sseError({ message: "boom" })), { appendLog });
    expect(appendLog).toHaveBeenCalledWith({ status: "FAILED 502" });
    expect(appendLog).not.toHaveBeenCalledWith(expect.objectContaining({ status: "200 OK" }));
  });

  it("OpenAI client gets the same non-2xx result", async () => {
    const result = await callHandler(sseResponse(sseError({ message: "boom" })), { sourceFormat: FORMATS.OPENAI });
    expect(result.success).toBe(false);
    expect(result.status).toBe(502);
  });

  it("normal SSE body is unchanged: Claude client gets an Anthropic message with usage", async () => {
    const result = await callHandler(sseResponse(okSSE));
    expect(result.success).toBe(true);
    const json = await result.response.json();
    expect(json.type).toBe("message");
    expect(json.content).toEqual([{ type: "text", text: "Tokyo" }]);
    expect(json.usage.input_tokens).toBeGreaterThan(0);
    expect(json.usage.output_tokens).toBe(2);
  });
});
