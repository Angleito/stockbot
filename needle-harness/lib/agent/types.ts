// Shared agent types. Single owner for Tool/Evidence/Event/Metrics contracts.

export type JSONSchema = {
  type: string;
  properties?: Record<string, unknown>;
  required?: string[];
  [k: string]: unknown;
};

export type Evidence = {
  id: string;
  source: string;
  title?: string;
  url?: string;
  retrievedAt: string;
  content: string;
};

export type ToolResult = { ok: true; evidence: Evidence } | { ok: false; error: string };

export type Tool = {
  description: string;
  parameters: JSONSchema;
  execute(args: Record<string, unknown>): Promise<ToolResult>;
};

export type AgentEvent =
  | { type: "agent_start"; prompt: string }
  | { type: "needle_decision"; step: number; tool: string | null; arguments: Record<string, unknown>; confidence: number | null }
  | { type: "tool_start"; tool: string }
  | { type: "tool_result"; tool: string; evidenceId?: string; preview: string }
  | { type: "reasoning_start"; model: string }
  | { type: "answer_delta"; text: string }
  | { type: "done"; metrics: Metrics }
  | { type: "error"; message: string };

export type Metrics = {
  totalMs: number;
  needle: { calls: number; totalMs: number; escalations: number };
  tools: { calls: number; totalMs: number };
  muse: {
    calls: number;
    inputTokens?: number;
    outputTokens?: number;
    cachedTokens?: number;
    cost?: number;
    totalMs: number;
  };
  evidence: { count: number; characters: number };
};
