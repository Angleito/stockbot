import { describe, expect, test } from "bun:test";
import { runAgent } from "./loop";
import type { RunAgentDeps } from "./loop";
import { makeEvidence } from "./evidence";
import type { AgentEvent, Metrics, Tool, ToolResult } from "./types";
import type { NeedleDecision } from "../needle/client";

const PROMPT = "NVDA demand outlook";
const ACTION_A: NeedleDecision = {
  tool: "search_web",
  arguments: { query: "NVDA demand" },
  confidence: 1,
  reasoning: "r",
  escalate: false,
};
const ACTION_B: NeedleDecision = {
  tool: "search_web",
  arguments: { query: "NVDA outlook" },
  confidence: 1,
  reasoning: "r",
  escalate: false,
};
const ESCALATE: NeedleDecision = { tool: null, arguments: {}, confidence: null, reasoning: "", escalate: true };
const DUPLICATE_ERROR =
  "duplicate_research_action: this exact action already ran with no new evidence; choose another action or escalate";

type StepFeedback = { ok: boolean; error: string };

function isStepFeedback(value: unknown): value is StepFeedback {
  if (typeof value !== "object" || value === null) return false;
  if (!("ok" in value) || typeof value.ok !== "boolean") return false;
  return !("error" in value) || typeof value.error === "string";
}

function requireFeedback(value: unknown): StepFeedback {
  if (!isStepFeedback(value) || !("error" in value)) throw new Error("expected Needle step feedback");
  return value;
}

function failedEvents(events: AgentEvent[], category: string): AgentEvent[] {
  return events.filter((e) => (e.type === "failed" || e.type === "tool_failed") && e.category === category);
}

function doneMetrics(events: AgentEvent[]): Metrics | null {
  const done = events.find((e) => e.type === "done");
  return done !== undefined && done.type === "done" ? done.metrics : null;
}

type SetupOpts = {
  start: () => Promise<NeedleDecision>;
  step: (result: unknown) => Promise<NeedleDecision>;
  toolImpl: (args: Record<string, unknown>) => Promise<ToolResult>;
};

type Harness = {
  events: AgentEvent[];
  stepCalls: unknown[];
  counts: { tools: number; reason: number };
  deps: RunAgentDeps;
};

function setup(opts: SetupOpts): Harness {
  const events: AgentEvent[] = [];
  const stepCalls: unknown[] = [];
  const counts = { tools: 0, reason: 0 };
  const toolSet: Record<string, Tool> = {
    search_web: {
      description: "search",
      parameters: { type: "object", properties: {} },
      execute: (args) => {
        counts.tools += 1;
        return opts.toolImpl(args);
      },
    },
  };
  const deps: RunAgentDeps = {
    acquireNeedle: () => Promise.resolve(() => { }),
    router: {
      start: opts.start,
      step: (result) => {
        stepCalls.push(result);
        return opts.step(result);
      },
    },
    toolSet,
    newSessionId: () => "test-session",
    endSession: () => Promise.resolve(),
    reason: () => {
      counts.reason += 1;
      return Promise.resolve({ text: "answer", usage: {} });
    },
  };
  return { events, stepCalls, counts, deps };
}

describe("runAgent control paths", () => {
  test("retryable failure reroutes to Needle and still synthesizes", async () => {
    let n = 0;
    const h = setup({
      start: () => Promise.resolve({ ...ACTION_A }),
      step: () => {
        n += 1;
        return Promise.resolve(n === 1 ? { ...ACTION_B } : { ...ESCALATE });
      },
      toolImpl: (args) => {
        if (args["query"] === "NVDA demand")
          return Promise.resolve({
            ok: false as const,
            error: "upstream timeout",
            category: "timeout" as const,
            retryable: true,
          });
        return Promise.resolve({ ok: true as const, evidence: makeEvidence("search_web", "NVDA outlook body") });
      },
    });
    await runAgent(PROMPT, (e) => h.events.push(e), { deps: h.deps });
    expect(h.stepCalls.length).toBe(2);
    const first = requireFeedback(h.stepCalls[0]);
    expect(first.ok).toBe(false);
    expect(first.error).toBe("upstream timeout");
    expect(h.counts.tools).toBe(2);
    expect(h.counts.reason).toBe(1);
    expect(h.events.some((e) => e.type === "reasoning_start")).toBe(true);
    expect(doneMetrics(h.events)?.muse.calls).toBe(1);
  });

  test("non-retryable failure never calls Needle again and skips Muse", async () => {
    const h = setup({
      start: () => Promise.resolve({ ...ACTION_A }),
      step: () => Promise.resolve({ ...ESCALATE }),
      toolImpl: () =>
        Promise.resolve({
          ok: false as const,
          error: "dispatch: session tool budget exhausted (1/1)",
          category: "tool_budget_exhausted" as const,
          retryable: false,
        }),
    });
    await runAgent(PROMPT, (e) => h.events.push(e), { deps: h.deps });
    expect(h.stepCalls.length).toBe(0);
    expect(h.counts.tools).toBe(1);
    expect(failedEvents(h.events, "tool_budget_exhausted").length).toBeGreaterThan(0);
    expect(failedEvents(h.events, "research_loop_detected").length).toBe(0);
    expect(h.counts.reason).toBe(0);
    expect(h.events.some((e) => e.type === "reasoning_start")).toBe(false);
    expect(h.events.some((e) => e.type === "answer_delta")).toBe(false);
    expect(doneMetrics(h.events)?.muse.calls).toBe(0);
    expect(doneMetrics(h.events)?.failures["tool_budget_exhausted"]).toBe(1);
  });

  test("deadline is terminal and skips Muse before any tool runs", async () => {
    const h = setup({
      start: () => Promise.resolve({ ...ACTION_A }),
      step: () => Promise.resolve({ ...ESCALATE }),
      toolImpl: () => Promise.resolve({ ok: true as const, evidence: makeEvidence("search_web", "body") }),
    });
    await runAgent(PROMPT, (e) => h.events.push(e), { deps: h.deps, workerDeadlineMs: 0 });
    expect(h.counts.tools).toBe(0);
    expect(h.counts.reason).toBe(0);
    expect(h.events.some((e) => e.type === "reasoning_start")).toBe(false);
    expect(doneMetrics(h.events)?.failures["deadline_exceeded"]).toBe(1);
    expect(doneMetrics(h.events)?.muse.calls).toBe(0);
  });

  test("first duplicate feeds back, second duplicate is terminal with no Muse", async () => {
    let n = 0;
    const h = setup({
      start: () => Promise.resolve({ ...ACTION_A }),
      step: () => {
        n += 1;
        return Promise.resolve({ ...ACTION_A });
      },
      toolImpl: () =>
        Promise.resolve({ ok: true as const, evidence: makeEvidence("search_web", "NVDA demand body") }),
    });
    await runAgent(PROMPT, (e) => h.events.push(e), { deps: h.deps });
    expect(n).toBe(2);
    expect(h.stepCalls.length).toBe(2);
    const second = requireFeedback(h.stepCalls[1]);
    expect(second.ok).toBe(false);
    expect(second.error).toBe(DUPLICATE_ERROR);
    expect(h.counts.tools).toBe(1);
    expect(failedEvents(h.events, "research_loop_detected").length).toBeGreaterThan(0);
    expect(h.counts.reason).toBe(0);
    expect(h.events.some((e) => e.type === "reasoning_start")).toBe(false);
    expect(doneMetrics(h.events)?.muse.calls).toBe(0);
  });

  test("provider down is terminal and skips Muse", async () => {
    const h = setup({
      start: () => Promise.reject(new Error("needle down")),
      step: () => Promise.resolve({ ...ESCALATE }),
      toolImpl: () => Promise.resolve({ ok: true as const, evidence: makeEvidence("search_web", "body") }),
    });
    await runAgent(PROMPT, (e) => h.events.push(e), { deps: h.deps });
    expect(h.counts.tools).toBe(0);
    expect(h.counts.reason).toBe(0);
    expect(failedEvents(h.events, "provider_error").length).toBeGreaterThan(0);
    expect(h.events.some((e) => e.type === "reasoning_start")).toBe(false);
    expect(doneMetrics(h.events)?.muse.calls).toBe(0);
  });

  test("normal escalation still synthesizes", async () => {
    const h = setup({
      start: () => Promise.resolve({ ...ESCALATE }),
      step: () => Promise.resolve({ ...ESCALATE }),
      toolImpl: () => Promise.resolve({ ok: true as const, evidence: makeEvidence("search_web", "body") }),
    });
    await runAgent(PROMPT, (e) => h.events.push(e), { deps: h.deps });
    expect(h.counts.reason).toBe(1);
    expect(h.events.some((e) => e.type === "reasoning_start")).toBe(true);
    expect(doneMetrics(h.events)?.muse.calls).toBe(1);
  });
});
