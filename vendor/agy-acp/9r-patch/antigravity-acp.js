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
    // "agy-acp" is the compose service name: 9router reaches the bridge over
    // the internal compose network. A non-container deployment must point this
    // at wherever the bridge runs (it publishes no host port by design).
    baseUrl: "http://agy-acp:20130/v1/chat/completions",
    format: "openai",
  },
  models: [
    { id: "gemini-3.8-flash-high", name: "Gemini 3.8 Flash (High)" },
    { id: "gemini-3.8-flash-medium", name: "Gemini 3.8 Flash (Medium)" },
    { id: "gemini-3.8-flash-low", name: "Gemini 3.8 Flash (Low)" },
    // Gemini 4 tiers: only on accounts entitled after the Gemini 4 rollout
    // (verified live against profile karere — completed turns with real
    // token accounting in the CLI conversation store, 2026-11-24).
    { id: "gemini-4-neon-high", name: "Gemini 4 Neon (High)" },
    { id: "gemini-4-hydrogen-high", name: "Gemini 4 Hydrogen (High)" },
    // Carbon tiers probed live (2026-11-26): accepted by the karere account
    // only, same entitlement split as Neon.
    { id: "gemini-4-carbon-high", name: "Gemini 4 Carbon (High)" },
    { id: "gemini-4-carbon-medium", name: "Gemini 4 Carbon (Medium)" },
    { id: "gemini-4-carbon-low", name: "Gemini 4 Carbon (Low)" },
    { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6 (Thinking)" },
    { id: "claude-opus-4-6-thinking", name: "Claude Opus 4.6 (Thinking)" },
    { id: "gpt-oss-120b-medium", name: "GPT-OSS 120B (Medium)" },
  ],
};
