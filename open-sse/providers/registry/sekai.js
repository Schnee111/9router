export default {
  id: "openai-compatible-chat-sekai",
  priority: 25,
  alias: "sekai",
  aliases: ["sk", "vibe"],
  uiAlias: "Sekai",
  display: {
    name: "Sekai (Vibe)",
    icon: "sparkles",
    color: "#8B5CF6",
    website: "https://vibe.madewgn.dev",
  },
  category: "apikey",
  transport: {
    baseUrl: "https://vibe.madewgn.dev/v1/chat/completions",
    validateUrl: "https://vibe.madewgn.dev/v1/models",
    reasoningInject: {
      scope: "all",
    },
  },
  models: [
    { id: "ds/deepseek-v4-pro", name: "DeepSeek V4 Pro" },
    { id: "ds/deepseek-v4-pro-max", name: "DeepSeek V4 Pro Max" },
    { id: "ds/deepseek-v4-pro-none", name: "DeepSeek V4 Pro (No Thinking)" },
    { id: "ds/deepseek-v4-flash", name: "DeepSeek V4 Flash" },
    { id: "ds/deepseek-chat", name: "DeepSeek Chat" },
    { id: "ds/deepseek-reasoner", name: "DeepSeek Reasoner" },
  ],
};
