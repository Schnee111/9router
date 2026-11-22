export default {
  id: "antigravity-acp",
  priority: 19,
  hasFree: true,
  alias: "agy-acp",
  uiAlias: "agy-acp",
  hidden: false,
  display: {
    name: "Antigravity ACP",
    icon: "rocket_launch",
    color: "#F59E0B",
    textIcon: "ACP",
    website: "https://antigravity.google",
    notice: {
      signupUrl: "https://antigravity.google",
      text: "Runs Google's own `agy acp` server (Agent Client Protocol) on 127.0.0.1:20130. Traffic carries the official Antigravity CLI fingerprint instead of a replayed OAuth token, which is what Google's ToS sweep flags. Manage accounts at http://<host>:20130/admin.",
    },
  },
  category: "free",
  authType: "none",
  noAuth: true,
  authModes: ["none"],
  transport: {
    // registry transport baseUrl is used verbatim — no path is appended,
    // so it must be the full chat endpoint, not just the /v1 root.
    baseUrl: "http://127.0.0.1:20130/v1/chat/completions",
    format: "openai",
  },
  models: [
    { id: "gemini-3.8-flash-high", name: "Gemini 3.8 Flash (High)" },
    { id: "gemini-3.8-flash-medium", name: "Gemini 3.8 Flash (Medium)" },
    { id: "gemini-3.8-flash-low", name: "Gemini 3.8 Flash (Low)" },
    { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6 (Thinking)" },
    { id: "claude-opus-4-6-thinking", name: "Claude Opus 4.6 (Thinking)" },
    { id: "gpt-oss-120b-medium", name: "GPT-OSS 120B (Medium)" },
  ],
};
