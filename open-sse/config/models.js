// Model metadata registry
// Only define models that differ from DEFAULT_MODEL_INFO
// Custom entries are merged over default
const DEFAULT_MODEL_INFO = {
  type: ["chat"],
  contextWindow: 200000,
};

export const MODEL_INFO = {
  // qoder-bridge Kimi K3 aliases — upstream Kimi K3 is 1M context (verified
  // via models.dev limit.context=1048576 and opencode/kimi-k3 upstream entry).
  // Without these entries the qd/* routes inherit the 200k default and
  // Hermes resolves them as 200k instead of 1M.
  "qd/kmodel_latest": { contextWindow: 1048576, maxOutput: 131072 },
  "qd/kmodel": { contextWindow: 1048576, maxOutput: 131072 },
  kmodel_latest: { contextWindow: 1048576, maxOutput: 131072 },
  kmodel: { contextWindow: 1048576, maxOutput: 131072 },
};

export function getModelInfo(modelId) {
  return { ...DEFAULT_MODEL_INFO, ...MODEL_INFO[modelId] };
}
