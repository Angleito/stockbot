import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { needleRouter } from "../needle/client";
import { reason, type MuseUsage } from "../muse/client";
import { tools } from "../tools/index";
import { resetEvidenceIds } from "./evidence";
import type { AgentEvent, Evidence, Metrics } from "./types";

export type NeedleDecisionRecord = {
  step: number;
  tool: string | null;
  arguments: Record<string, unknown>;
  confidence: number | null;
  reasoning: string;
};

export type ToolCallRecord = { tool: string; ok: boolean; evidenceId?: string };

const MUSE_MODEL = "muse-spark-1.3-contributor";

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

function isConversational(prompt: string): boolean {
  const n = prompt.trim().toLowerCase().replace(/[!.,?…]+$/u, "").replace(/\s+/g, " ");
  return /^(hi|hello|hey|yo|sup|hiya|howdy|good morning|good afternoon|good evening|how are you|how's it going|what's up|whats up|thanks|thank you|thx|bye|goodbye|see you|see ya)$/.test(n);
}

export async function runAgent(
  prompt: string,
  emit: (e: AgentEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const t0 = performance.now();
  const startedAt = new Date().toISOString();
  resetEvidenceIds();
  const evidence: Evidence[] = [];
  const needleDecisions: NeedleDecisionRecord[] = [];
  const toolCalls: ToolCallRecord[] = [];
  let needleMs = 0;
  let toolMs = 0;
  let escalations = 0;
  let escalated = false;
  let directFallback = false;

  emit({ type: "agent_start", prompt });
  if (isConversational(prompt)) {
    const answer = "Hi! I'm Needle. Ask me something to look up \u2014 the time, SEC filings, a URL to fetch, or a web search.";
    emit({ type: "answer_delta", text: answer });
    const metrics: Metrics = {
      totalMs: performance.now() - t0,
      needle: { calls: 0, totalMs: 0, escalations: 0 },
      tools: { calls: 0, totalMs: 0 },
      muse: { calls: 0, totalMs: 0 },
      evidence: { count: 0, characters: 0 },
    };
    emit({ type: "done", metrics });
    if (process.env.BENCHMARK === "1") {
      await mkdir(join(process.cwd(), "benchmarks"), { recursive: true });
      await writeFile(
        join(process.cwd(), "benchmarks", `${stamp()}-${Math.random().toString(36).slice(2, 8)}.json`),
        JSON.stringify({ prompt, startedAt, needleDecisions, toolCalls, evidence, museUsage: {}, answer, metrics }, null, 2),
      );
    }
    return;
  }
  const seen = new Set<string>();
  const history: string[] = [];
  let consecutiveFailures = 0;
  for (let step = 0; step < 8; step++) {
    if (opts?.signal?.aborted) break;
    const context =
      "If no more retrieval is needed, return no tool call.\n" +
      (history.length ? `Prior calls (do not repeat ANY listed call - if evidence suffices, return no tool call):\n${history.slice(-4).join("\n")}\n` : "") +
      evidence.map((e) => `${e.id}:${e.source}`).join(", ") +
      "\n" +
      evidence.slice(-2).map((e) => e.content.slice(0, 1000)).join("\n");
    const tn = performance.now();
    let decision;
    try {
      decision = await needleRouter.route({ prompt, context });
    } catch {
      escalations += 1;
      if (evidence.length === 0) escalated = true;
      directFallback = evidence.length === 0;
      break;
    }
    needleMs += performance.now() - tn;
    needleDecisions.push({
      step,
      tool: decision.tool,
      arguments: decision.arguments,
      confidence: decision.confidence,
      reasoning: decision.reasoning,
    });
    emit({ type: "needle_decision", step, tool: decision.tool, arguments: decision.arguments, confidence: decision.confidence });
    if (decision.escalate || !decision.tool) {
      escalations += 1;
      if (evidence.length === 0) escalated = true;
      break;
    }
    const stripped = prompt.trim().toLowerCase().replace(/[^a-z0-9]+/g, "");
    const timeWord = /\b(time|clock|date|today|now|utc|hour|minute|second|day|week|month|year|morning|afternoon|evening)\b/i.test(prompt);
    if (stripped.length < 2 || (decision.tool === "get_current_time" && !timeWord)) {
      escalations += 1;
      if (evidence.length === 0) escalated = true;
      directFallback = evidence.length === 0;
      break;
    }
    const tool = tools[decision.tool];
    if (!tool) {
      escalations += 1;
      if (evidence.length === 0) escalated = true;
      directFallback = evidence.length === 0;
      break;
    }
    const key = decision.tool + JSON.stringify(decision.arguments);
    if (seen.has(key)) break;
    seen.add(key);
    emit({ type: "tool_start", tool: decision.tool });
    const tt = performance.now();
    const result = await tool.execute(decision.arguments);
    toolMs += performance.now() - tt;
    toolCalls.push({ tool: decision.tool, ok: result.ok, evidenceId: result.ok ? result.evidence.id : undefined });
    if (!result.ok) {
      emit({ type: "tool_result", tool: decision.tool, preview: result.error.slice(0, 160) });
      history.push(`${decision.tool}${JSON.stringify(decision.arguments)} -> FAILED: ${result.error.slice(0, 120)}`);
      consecutiveFailures += 1;
      if (consecutiveFailures >= 2) {
        escalations += 1;
        if (evidence.length === 0) escalated = true;
        break;
      }
      continue;
    }
    consecutiveFailures = 0;
    evidence.push(result.evidence);
    history.push(`${decision.tool}${JSON.stringify(decision.arguments)} -> ok ${result.evidence.id}`);
    emit({ type: "tool_result", tool: decision.tool, evidenceId: result.evidence.id, preview: result.evidence.content.slice(0, 160) });
    if (evidence.length >= 5 || evidence.reduce((n, e) => n + e.content.length, 0) >= 24000) break;
  }

  emit({ type: "reasoning_start", model: MUSE_MODEL });
  const tm = performance.now();
  let answer: string;
  let usage: MuseUsage;
  try {
    const r = await reason({ prompt, evidence, escalated: escalated && !directFallback, direct: directFallback && evidence.length === 0, onDelta: (text) => emit({ type: "answer_delta", text }) });
    answer = r.text;
    usage = r.usage;
  } catch (err) {
    emit({ type: "error", message: err instanceof Error ? err.message : String(err) });
    return;
  }
  const museMs = performance.now() - tm;
  const metrics: Metrics = {
    totalMs: performance.now() - t0,
    needle: { calls: needleDecisions.length, totalMs: needleMs, escalations },
    tools: { calls: toolCalls.length, totalMs: toolMs },
    muse: {
      calls: 1,
      inputTokens: usage.inputTokens,
      outputTokens: usage.outputTokens,
      cachedTokens: usage.cachedTokens,
      cost: usage.cost,
      totalMs: museMs,
    },
    evidence: { count: evidence.length, characters: evidence.reduce((n, e) => n + e.content.length, 0) },
  };
  emit({ type: "done", metrics });

  if (process.env.BENCHMARK === "1") {
    await mkdir(join(process.cwd(), "benchmarks"), { recursive: true });
    await writeFile(
      join(process.cwd(), "benchmarks", `${stamp()}-${Math.random().toString(36).slice(2, 8)}.json`),
      JSON.stringify({ prompt, startedAt, needleDecisions, toolCalls, evidence, museUsage: usage, answer, metrics }, null, 2),
    );
  }
}
