import type { RunKernelOpts } from "@/lib/agent/kernel";
import type { AgentEvent, EvaluationTrace } from "@/lib/agent/types";
import { describe, expect, mock, test } from "bun:test";

// ponytail: route boundary only — auth gate, event order, asOf forwarding.

const calls: Array<{ prompt: string; opts?: RunKernelOpts }> = [];

const kernelMock = mock(async (prompt: string, emit: (e: AgentEvent) => void, opts?: RunKernelOpts): Promise<void> => {
  calls.push({ prompt, opts });
  emit({ type: "agent_start", prompt });
  emit({ type: "answer_delta", text: "hello" });
  if (opts?.includeTrace) {
    emit({ type: "evaluation_trace", trace: { ...FULL_TRACE } });
  }
  emit({
    type: "done",
    metrics: {
      totalMs: 1,
      needle: { calls: 0, totalMs: 0, escalations: 0 },
      tools: { calls: 0, totalMs: 0 },
      muse: { calls: 0, totalMs: 0 },
      evidence: { count: 0, characters: 0 },
      failures: {},
    },
  });
});

const FULL_TRACE: EvaluationTrace = {
  session: { session_id: "rs:1" },
  sessionId: "rs:1",
  asOf: "2024-01-15",
  objective: "q?",
  evidence: [{ evidence_id: "ev-1", content: "fact", provenance: { kind: "finra_record" }, claim_kind: "observed_fact" }],
  nodes: [{ node_id: "rn:1" }],
  nodeRecords: [{ node_id: "rn:1" }],
  decisions: [{ decision_id: "dec:1" }],
  unresolved: [],
  incompleteGuard: false,
  guardState: { incompleteGuard: false, escalated: false },
  attempts: [{ tool: "query_finra", arguments: { token: "[REDACTED]" } }],
  toolExecutions: [{ step: 0, tool: "query_finra", arguments: { token: "[REDACTED]" } }],
  toolCalls: [{ tool: "query_finra", ok: true }],
  failures: {},
  escalations: 0,
  escalated: false,
  jobs: [{ job_id: "job:1" }],
  events: [{ event_type: "job.started" }],
  dossiers: [{ dossier_id: "d:1", findings: [{ text: "f", evidence_ids: ["ev-1"] }] }],
  claims: [{ text: "f" }],
  coverageArtifacts: [{ artifact_id: "cov:1" }],
  toolResults: [{ tool_result_id: "tr:1" }],
  modelPrompt: { objective: "q?", nodes: 1, decisions: 1, unresolved: 0 },
};

mock.module("@/lib/agent/kernel", () => ({ runKernelAgent: kernelMock }));
// Exception: mock.module only affects subsequent imports, so the route under
// test is loaded dynamically after mocking (module-loading boundary test).
const { POST } = await import("@/app/api/agent/route");

function post(body: unknown, token?: string): Promise<Response> {
  return POST(
    new Request("http://x/api/agent", {
      method: "POST",
      ...(token !== undefined ? { headers: { authorization: `Bearer ${token}` } } : {}),
      body: JSON.stringify(body),
    }),
  );
}

async function readEvents(res: Response): Promise<AgentEvent[]> {
  const text = await res.text();
  const out: AgentEvent[] = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("data: ")) out.push(JSON.parse(line.slice(6)) as AgentEvent);
  }
  return out;
}

function tracesOf(events: AgentEvent[]): EvaluationTrace[] {
  const out: EvaluationTrace[] = [];
  for (const e of events) if (e.type === "evaluation_trace") out.push(e.trace);
  return out;
}


describe("agent route evaluation trace boundary", () => {
  test("missing token on includeTrace returns 403 before execution", async () => {
    calls.length = 0;
    delete process.env.STOCKBOT_EVAL_TOKEN;
    const res = await post({ prompt: "q?", includeTrace: true });
    expect(res.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  test("blank token denies; wrong bearer denies", async () => {
    calls.length = 0;
    process.env.STOCKBOT_EVAL_TOKEN = "  ";
    expect((await post({ prompt: "q?", includeTrace: true }, "s3cret")).status).toBe(403);
    process.env.STOCKBOT_EVAL_TOKEN = "s3cret";
    expect((await post({ prompt: "q?", includeTrace: true }, "nope")).status).toBe(403);
    expect(calls).toHaveLength(0);
    delete process.env.STOCKBOT_EVAL_TOKEN;
  });

  test("normal mode has no trace event", async () => {
    delete process.env.STOCKBOT_EVAL_TOKEN;
    const res = await post({ prompt: "q?" });
    expect(res.status).toBe(200);
    const events = await readEvents(res);
    expect(tracesOf(events)).toHaveLength(0);
    expect(events.at(-1)?.type).toBe("done");
  });

  test("authorized mode emits one full trace before done and preserves answer events", async () => {
    calls.length = 0;
    process.env.STOCKBOT_EVAL_TOKEN = "s3cret";
    try {
      const res = await post({ prompt: "q?", asOf: "2024-01-15", includeTrace: true }, "s3cret");
      expect(res.status).toBe(200);
      expect(calls).toHaveLength(1);
      expect(calls[0]?.opts?.asOf).toBe("2024-01-15");
      const events = await readEvents(res);
      const traces = tracesOf(events);
      expect(traces).toHaveLength(1);
      const trace = traces[0];
      if (trace === undefined) throw new Error("expected one trace");
      for (const key of [
        "session",
        "jobs",
        "events",
        "evidence",
        "nodes",
        "decisions",
        "dossiers",
        "claims",
        "coverageArtifacts",
        "toolResults",
        "guardState",
      ] as const) {
        expect(trace).toHaveProperty(key);
      }
      const types = events.map((e) => e.type);
      expect(types.indexOf("evaluation_trace")).toBeLessThan(types.indexOf("done"));
      expect(types).toContain("answer_delta");
      expect(types.at(-1)).toBe("done");
    } finally {
      delete process.env.STOCKBOT_EVAL_TOKEN;
    }
  });

});
