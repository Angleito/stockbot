import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { acquireNeedle, needleRouter, type NeedleDecision } from "../needle/client";
import { reason, type MuseUsage } from "../muse/client";
import { tools } from "../tools/index";
import { groundingError } from "./grounding";
import { redactArgs } from "./types";
import type { AgentEvent, Evidence, FailureCategory, Metrics } from "./types";
import { endSession, newSessionId } from "../tools/stockbot";

export type NeedleDecisionRecord = {
  step: number;
  tool: string | null;
  arguments: Record<string, unknown>;
  confidence: number | null;
  reasoning: string;
};

export type ToolCallRecord = { tool: string; ok: boolean; evidenceId?: string };

const MUSE_MODEL = "muse-spark-1.3-contributor";
// INTERIM (Phase5 removal): TS worker loop until kernel-owned scheduler lands (plan §18/Phase5). Holds step IDs/refs + display buffer only, never kernel truth. Stops on escalate/unknown-tool/duplicate/2-strike/provider-down/abort/muse-result only — no research-depth caps.
// ponytail: operational guard is per-request timeouts (TOOL_TIMEOUT_MS/bridge 120s) + opts.signal abort (§4), not caps. evidence[] is interim display buffer (Muse formatEvidence truncates oldest-first to 24k); DISPLAY_CHAR_LIMIT is evidenceText() view slice only (§23 retrieval vs display), never loop exit.
const DISPLAY_CHAR_LIMIT = 24000;

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
  const evidence: Evidence[] = [];
  const needleDecisions: NeedleDecisionRecord[] = [];
  const toolCalls: ToolCallRecord[] = [];
  const failures: Partial<Record<FailureCategory, number>> = {};
  // ponytail: no research-depth caps here (§4/§21: null=unlimited, stop on sufficiency/loop/escalation only). DISPLAY_CHAR_LIMIT below is a model-view bound (§23), never termination.
  let needleMs = 0;
  let toolMs = 0;
  let escalations = 0;
  let escalated = false;
  let directFallback = false;

  const countFailure = (c: FailureCategory): void => {
    failures[c] = (failures[c] ?? 0) + 1;
  };

  const evidenceText = (): string => {
    const text = evidence.map((e) => e.content).join("\n");
    return text.slice(0, DISPLAY_CHAR_LIMIT);
  };

  const noteNeedleDown = (err: unknown): void => {
    const msg = err instanceof Error ? err.message : String(err);
    countFailure("provider_error");
    emit({ type: "tool_failed", tool: "needle", category: "provider_error", preview: msg.slice(0, 160) });
    escalations += 1;
    if (evidence.length === 0) escalated = true;
    directFallback = evidence.length === 0;
  };

  const loopDetected = (tool: string, msg: string): void => {
    countFailure("research_loop_detected");
    emit({ type: "tool_failed", tool, category: "research_loop_detected", preview: msg.slice(0, 160) });
    emit({ type: "failed", category: "research_loop_detected", message: msg.slice(0, 160) });
    escalations += 1;
    if (evidence.length === 0) escalated = true;
  };

  emit({ type: "agent_start", prompt });
  if (isConversational(prompt)) {
    const answer = "Hi! I'm Needle. Ask me something to look up — the time, SEC filings, or a web search.";
    emit({ type: "answer_delta", text: answer });
    const metrics: Metrics = {
      totalMs: performance.now() - t0,
      needle: { calls: 0, totalMs: 0, escalations: 0 },
      tools: { calls: 0, totalMs: 0 },
      muse: { calls: 0, totalMs: 0 },
      evidence: { count: 0, characters: 0 },
      failures: {},
    };
    emit({ type: "done", metrics });
    if (process.env.BENCHMARK === "1") {
      try {
        await mkdir(join(process.cwd(), "benchmarks"), { recursive: true });
        await writeFile(
          join(process.cwd(), "benchmarks", `${stamp()}-${Math.random().toString(36).slice(2, 8)}.json`),
          JSON.stringify({ prompt, startedAt, needleDecisions, toolCalls, evidence, museUsage: {}, answer, metrics }, null, 2),
        );
      } catch {
        // ponytail: telemetry never changes research behavior (§10 recorder rule); benchmark dump is best-effort.
      }
    }
    return;
  }
  // ponytail: transport UUID only; kernel research.session.create deferred until job/evidence/finalize wiring lands (else every run orphans a kernel session+job).
  const sessionId = newSessionId();
  const releaseNeedle = await acquireNeedle();
  try {
    const seen = new Set<string>();
    let consecutiveFailures = 0;
    let decision: NeedleDecision | null = null;
    try {
      const tn = performance.now();
      decision = await needleRouter.start(prompt);
      needleMs += performance.now() - tn;
    } catch (err) {
      noteNeedleDown(err);
      decision = null;
    }
    for (let step = 0; decision; step++) {
      if (opts?.signal?.aborted) break;
      const current = decision;
      decision = null;
      const redacted = redactArgs(current.arguments);
      needleDecisions.push({
        step,
        tool: current.tool,
        arguments: redacted,
        confidence: current.confidence,
        reasoning: current.reasoning,
      });
      emit({ type: "needle_decision", step, tool: current.tool, arguments: redacted, confidence: current.confidence });
      if (current.escalate || !current.tool) {
        escalations += 1;
        if (evidence.length === 0) escalated = true;
        break;
      }
      const stripped = prompt.trim().toLowerCase().replace(/[^a-z0-9]+/g, "");
      const timeWord = /\b(time|clock|date|today|now|utc|hour|minute|second|day|week|month|year|morning|afternoon|evening)\b/i.test(prompt);
      if (stripped.length < 2 || (current.tool === "get_current_time" && !timeWord)) {
        escalations += 1;
        if (evidence.length === 0) escalated = true;
        directFallback = evidence.length === 0;
        break;
      }
      const tool = tools[current.tool];
      if (!tool) {
        escalations += 1;
        if (evidence.length === 0) escalated = true;
        directFallback = evidence.length === 0;
        break;
      }
      const key = current.tool + JSON.stringify(current.arguments);
      if (seen.has(key)) {
        countFailure("duplicate_research_action");
        emit({ type: "tool_failed", tool: current.tool, category: "duplicate_research_action", preview: key.slice(0, 160) });
        consecutiveFailures += 1;
        if (consecutiveFailures >= 2) loopDetected(current.tool, key);
        break;
      }
      seen.add(key);
      const rejection = groundingError(current.tool, current.arguments, {
        prompt,
        evidenceText: evidenceText(),
      });
      if (rejection) {
        countFailure("policy_rejection");
        emit({ type: "tool_result", tool: current.tool, preview: rejection.slice(0, 160) });
        emit({ type: "tool_failed", tool: current.tool, category: "policy_rejection", preview: rejection.slice(0, 160) });
        toolCalls.push({ tool: current.tool, ok: false });
        consecutiveFailures += 1;
        if (consecutiveFailures >= 2) {
          loopDetected(current.tool, rejection);
          break;
        }
        try {
          const tn = performance.now();
          decision = await needleRouter.step({ tool: current.tool, arguments: current.arguments, ok: false, error: rejection });
          needleMs += performance.now() - tn;
        } catch (err) {
          noteNeedleDown(err);
          break;
        }
        continue;
      }
      emit({ type: "tool_start", tool: current.tool });
      const tt = performance.now();
      const result = await tool.execute(current.arguments, { sessionId });
      toolMs += performance.now() - tt;
      toolCalls.push({ tool: current.tool, ok: result.ok, evidenceId: result.ok ? result.evidence.id : undefined });
      if (!result.ok) {
        countFailure(result.category);
        emit({ type: "tool_result", tool: current.tool, preview: result.error.slice(0, 160) });
        emit({ type: "tool_failed", tool: current.tool, category: result.category, preview: result.error.slice(0, 160) });
        consecutiveFailures += 1;
        if (consecutiveFailures >= 2) {
          loopDetected(current.tool, result.error);
          break;
        }
        try {
          const tn = performance.now();
          decision = await needleRouter.step({ tool: current.tool, arguments: current.arguments, ok: false, error: result.error });
          needleMs += performance.now() - tn;
        } catch (err) {
          noteNeedleDown(err);
          break;
        }
        continue;
      }
      consecutiveFailures = 0;
      evidence.push(result.evidence);
      emit({ type: "tool_result", tool: current.tool, evidenceId: result.evidence.id, preview: result.evidence.content.slice(0, 160) });
      // ponytail: no count termination; loop stops on escalate/duplicate/2-strike/needle-down/muse result.
      try {
        const tn = performance.now();
        decision = await needleRouter.step({ tool: current.tool, arguments: current.arguments, ok: true, evidence: result.evidence });
        needleMs += performance.now() - tn;
      } catch (err) {
        noteNeedleDown(err);
        break;
      }
    }
  } finally {
    releaseNeedle();
    await endSession(sessionId);
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
    const msg = err instanceof Error ? err.message : String(err);
    countFailure("provider_error");
    emit({ type: "tool_failed", tool: "muse", category: "provider_error", preview: msg.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: msg.slice(0, 160) });
    emit({ type: "error", message: msg });
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
    failures,
  };
  emit({ type: "done", metrics });

  if (process.env.BENCHMARK === "1") {
    try {
      await mkdir(join(process.cwd(), "benchmarks"), { recursive: true });
      await writeFile(
        join(process.cwd(), "benchmarks", `${stamp()}-${Math.random().toString(36).slice(2, 8)}.json`),
        JSON.stringify({ prompt, startedAt, needleDecisions, toolCalls, evidence, museUsage: usage, answer, metrics }, null, 2),
      );
    } catch {
      // ponytail: telemetry never changes research behavior (§10 recorder rule); benchmark dump is best-effort.
    }
  }
}
