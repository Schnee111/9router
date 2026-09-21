export default {
  id: "openai-compatible-chat-inferx",
  priority: 25,
  alias: "inferx",
  aliases: ["ix", "infer"],
  uiAlias: "InferX",
  display: { name: "InferX", icon: "sparkles", color: "#F59E0B", website: "https://model.inferx.net" },
  category: "apikey",
  transport: {
    baseUrl: "https://model.inferx.net/endpoints/v1/chat/completions",
    validateUrl: "https://model.inferx.net/endpoints/v1/models",
  },
  models: [
    { id: "deepseek-v4-flash-0731", name: "DeepSeek V4 Flash 0731" },
    { id: "deepseek-v4-flash", name: "DeepSeek V4 Flash" },
  ],
};
