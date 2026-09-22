import { describe, expect, mock, test } from "bun:test";
import type { Evidence } from "@/lib/agent/types";

// Mutable stubs reconfigured per test; route.ts is imported dynamically after
// mock.module setup because static imports would bind the real kernel/muse
// modules before the mocks install (module-loading boundary test).
let winner: unknown = "reasoning_required";
let routeDown = false;
const runCalls: unknown[][] = [];
type ReasonCall = { prompt: string; evidence: Evidence[]; onDelta?: (t: string) => void };
const reasonCalls: ReasonCall[] = [];
type NeedleCall = { tool: string; schema?: unknown; objective?: unknown };
const needleCalls: NeedleCall[] = [];
const invoked: unknown[][] = [];
const ended: string[] = [];

mock.module("@/lib/agent/kernel", () => ({
  kernelRouter: {
    call: async () => {
      if (routeDown) throw new Error("worker down");
      return { route: winner };
    },
  },
  runKernelAgent: async (...args: unknown[]) => {
    runCalls.push(args);
  },
}));

mock.module("@/lib/muse/client", () => ({
  reason: async (opts: ReasonCall) => {
    reasonCalls.push(opts);
    opts.onDelta?.("hi");
    return { text: "hi", usage: {} };
  },
}));

mock.module("@/lib/needle/client", () => ({
  needleRouter: {
    generateArguments: async (req: NeedleCall) => {
      needleCalls.push(req);
      return { tool: req.tool, arguments: { q: "x" }, confidence: 1, reasoning: "t" };
    },
  },
}));

mock.module("@/lib/tools/stockbot", () => ({
  invoke: async (name: string, args: unknown, sessionId: string) => {
    invoked.push([name, args, sessionId]);
    return { ok: true, evidence: { id: "ev:bridge", source: name, retrievedAt: new Date().toISOString(), content: "bridged" } };
  },
  newSessionId: () => "sess-test",
  endSession: async (id: string) => {
    ended.push(id);
  },
}));

function reset(): void {
  winner = "reasoning_required";
  routeDown = false;
  runCalls.length = 0;
  reasonCalls.length = 0;
  needleCalls.length = 0;
  invoked.length = 0;
  ended.length = 0;
}

type AgentEventShape = { type: string;[k: string]: unknown };

async function eventsFor(prompt: string): Promise<AgentEventShape[]> {
  const { POST } = await import("./route");
  const req = new Request("http://localhost/api/agent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  const res = await POST(req);
  const text = await res.text();
  return text
    .split("\n\n")
    .filter((c) => c.trim())
    .map((c) => JSON.parse(c.replace(/^data: /, "")) as AgentEventShape);
}

function metricCalls(events: AgentEventShape[], key: string): unknown {
  const done = events.find((e) => e.type === "done");
  if (!done) throw new Error("missing done event");
  const metrics = done.metrics;
  if (!metrics || typeof metrics !== "object" || !(key in metrics)) throw new Error(`missing done metrics ${key}`);
  const section = (metrics as Record<string, unknown>)[key];
  if (!section || typeof section !== "object" || !("calls" in section)) throw new Error(`missing ${key} calls`);
  return section.calls;
}

describe("agent entry route", () => {
  test("reasoning_required answers direct without tools or research", async () => {
    reset();
    const types = (await eventsFor("hello")).map((e) => e.type);
    expect(types).toEqual(["agent_start", "reasoning_start", "answer_delta", "done"]);
    expect(runCalls.length).toBe(0);
    expect(needleCalls.length).toBe(0);
    expect(reasonCalls[0].evidence).toEqual([]);
  });
  test("reasoning-style prompts answer direct when routed reasoning_required", async () => {
    for (const prompt of [
      "Explain DCF valuation in simple terms",
      "What does share dilution mean for existing holders?",
      "Explain calls vs puts",
    ]) {
      reset();
      const types = (await eventsFor(prompt)).map((e) => e.type);
      expect(types).toEqual(["agent_start", "reasoning_start", "answer_delta", "done"]);
      expect(runCalls.length).toBe(0);
      expect(needleCalls.length).toBe(0);
      expect(reasonCalls[0].evidence).toEqual([]);
    }
  });

  test("exact local tool executes once then answers with evidence", async () => {
    reset();
    winner = "get_current_time";
    const events = await eventsFor("what time is it?");
    const types = events.map((e) => e.type);
    expect(types).toEqual(["agent_start", "needle_decision", "tool_start", "tool_result", "reasoning_start", "answer_delta", "done"]);
    expect(events.filter((e) => e.type === "tool_start").length).toBe(1);
    expect(runCalls.length).toBe(0);
    expect(needleCalls[0].tool).toBe("get_current_time");
    expect(reasonCalls[0].evidence.length).toBe(1);
    expect(ended).toEqual(["sess-test"]);
    expect(metricCalls(events, "tools")).toBe(1);
  });

  test("tool missing from TS registry executes via bridge invoke", async () => {
    reset();
    winner = "query_finra";
    const events = await eventsFor("short interest?");
    expect(events.filter((e) => e.type === "tool_start").length).toBe(1);
    expect(invoked.length).toBe(1);
    expect(invoked[0][0]).toBe("query_finra");
    expect(runCalls.length).toBe(0);
    expect(reasonCalls[0].evidence.length).toBe(1);
  });

  test("research_required runs the kernel agent", async () => {
    reset();
    winner = "research_required";
    await eventsFor("What drove NVDA revenue?");
    expect(runCalls.length).toBe(1);
    expect(reasonCalls.length).toBe(0);
  });

  test("malformed winner fails open to research", async () => {
    reset();
    winner = "search web; rm -rf";
    await eventsFor("What drove NVDA revenue?");
    expect(runCalls.length).toBe(1);
    expect(reasonCalls.length).toBe(0);
  });

  test("route outage fails open to research", async () => {
    reset();
    routeDown = true;
    await eventsFor("hello");
    expect(runCalls.length).toBe(1);
  });
});
