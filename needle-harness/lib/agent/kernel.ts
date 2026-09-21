import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { reason, type MuseUsage } from "../muse/client";
import { redactArgs } from "./types";
import type { AgentEvent, EvaluationTrace, Evidence, FailureCategory, Metrics } from "./types";

// KernelTrace is nominal so structural mismatches surface in typecheck rather than SSE runtime.
export type KernelTrace = EvaluationTrace & { readonly __kernelTraceBrand?: never };

// ponytail: one-shot worker per request (no persistent bridge); spawn/exit/stderr handling mirrors lib/needle/client.ts.
const MUSE_MODEL = "muse-spark-1.3-contributor";
const WORKER_TIMEOUT_MS = 10 * 60 * 1000;

// next dev runs with cwd=needle-harness; the repo root is its parent.
const ROOT = process.env.STOCKBOT_REPO_ROOT ?? join(process.cwd(), "..");
const WORKER = join(ROOT, "app/research/kernel_worker.py");
const VENV_PYTHON = `${homedir()}/.cache/needle-harness/.needle/bin/python`;
// Stockbot kernel needs repo deps (dotenv, app.*); the needle-only venv lacks them.
const REPO_PYTHON = join(ROOT, "venv/bin/python");

export type KernelEvidence = {
  id: string;
  content: string;
  source?: string;
  title?: string;
  url?: string;
  retrievedAt?: string;
};

export type KernelNeedleDecision = {
  step: number;
  tool: string | null;
  arguments: Record<string, unknown>;
  confidence: number | null;
  reasoning?: string;
};

export type KernelToolCall = {
  tool: string;
  ok: boolean;
  evidenceId?: string;
  error?: string;
  category?: FailureCategory;
};

export type KernelGraphNode = {
  node_id: string;
  question: string;
  status: string;
  depends_on: string[];
};

export type KernelReasonInput = {
  prompt: string;
  evidence: Evidence[];
  escalated: boolean;
  direct?: boolean;
  objective: string;
  nodes: KernelGraphNode[];
  decisions: Record<string, unknown>[];
  unresolved: string[];
  incompleteGuard: boolean;
  onDelta: (text: string) => void;
};

export type KernelResponse = {
  id?: string;
  objective?: string;
  session?: Record<string, unknown> | null;
  sessionId?: string;
  asOf?: string | null;
  evidence: KernelEvidence[];
  evidenceRecords?: Record<string, unknown>[];
  jobs?: Record<string, unknown>[];
  events?: Record<string, unknown>[];
  dossiers?: Record<string, unknown>[];
  coverageArtifacts?: Record<string, unknown>[];
  toolResults?: Record<string, unknown>[];
  nodes?: KernelGraphNode[];
  nodeRecords?: Record<string, unknown>[];
  decisions?: Record<string, unknown>[];
  unresolved?: string[];
  incompleteGuard?: boolean;
  incomplete_guard?: boolean;
  attempts?: Record<string, unknown>[];
  toolExecutions?: KernelNeedleDecision[];
  needleDecisions: KernelNeedleDecision[];
  toolCalls: KernelToolCall[];
  failures: Record<string, number>;
  escalations: number;
  escalated: boolean;
  error?: string;
  terminal?: { category: FailureCategory; message: string };
};

export type KernelChild = {
  stdin: { write: (data: string, cb?: (err?: Error | null) => void) => void } | null;
  stdout: { on: (event: "data", listener: (chunk: Buffer) => void) => void } | null;
  stderr: { on: (event: "data", listener: (chunk: Buffer) => void) => void } | null;
  on: (event: "error" | "exit", listener: (arg?: unknown) => void) => void;
  kill: (signal?: string) => void;
  readonly exitCode: number | null;
};

export type KernelSpawn = (cmd: string, args: string[], opts: { env: NodeJS.ProcessEnv }) => KernelChild;

export type KernelReasonResult = { text: string; usage: MuseUsage; missingEvidence?: string };
export type KernelReasonFn = (opts: KernelReasonInput) => Promise<KernelReasonResult>;
export type RunKernelDeps = {
  spawnFn?: KernelSpawn;
  reason?: KernelReasonFn;
  python?: string;
  workerPath?: string;
  timeoutMs?: number;
};
export type RunKernelOpts = {
  signal?: AbortSignal;
  deadlineMs?: number;
  asOf?: string | null;
  includeTrace?: boolean;
  onTrace?: (trace: KernelTrace) => void;
  deps?: RunKernelDeps;
};

function defaultSpawn(cmd: string, args: string[], opts: { env: NodeJS.ProcessEnv }): KernelChild {
  return spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"], env: opts.env }) as unknown as KernelChild;
}

function callWorker(
  body: Record<string, unknown>,
  opts: { signal?: AbortSignal; timeoutMs: number; python: string; workerPath: string; spawnFn: KernelSpawn },
): Promise<KernelResponse> {
  return new Promise<KernelResponse>((resolve, reject) => {
    let child: KernelChild;
    try {
      child = opts.spawnFn(opts.python, [opts.workerPath], {
        env: { ...process.env, NEEDLE_TELEMETRY: "0", DO_NOT_TRACK: "1" },
      });
    } catch (err) {
      reject(err);
      return;
    }
    console.error(`[ai] kernel worker spawn ${opts.python} (${opts.workerPath})`);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stderrTail = "";
    let buf = "";
    let settled = false;
    const done = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
      fn();
    };
    const fail = (err: Error): void => {
      done(() => {
        try {
          child.kill();
        } catch {
          // Already gone; rejection below carries the error.
        }
        reject(err);
      });
    };
    const onAbort = (): void => fail(new Error("worker aborted"));
    timer = setTimeout(() => fail(new Error(`kernel worker timeout; stderr tail: ${stderrTail || "(empty)"}`)), opts.timeoutMs);
    if (opts.signal?.aborted) {
      fail(new Error("worker aborted"));
      return;
    }
    opts.signal?.addEventListener("abort", onAbort, { once: true });
    child.stderr?.on("data", (d: Buffer) => {
      const s = d.toString();
      stderrTail = (stderrTail + s).slice(-2000);
      process.stderr.write(s.startsWith("[kernel-worker]") ? s : `[kernel-worker] ${s}`);
    });
    child.on("error", (err) => fail(err instanceof Error ? err : new Error(String(err))));
    child.on("exit", () => fail(new Error(`kernel worker exited; stderr tail: ${stderrTail || "(empty)"}`)));
    child.stdout?.on("data", (d: Buffer) => {
      buf += d.toString();
      let i = buf.indexOf("\n");
      while (i >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line) {
          let msg: unknown;
          try {
            msg = JSON.parse(line);
          } catch {
            // Non-JSON bridge noise; keep scanning.
          }
          if (typeof msg === "object" && msg !== null && (!("id" in msg) || msg.id === body.id)) {
            // boundary: JSONL line from our own worker; every field is defaulted where read below.
            const res: KernelResponse = msg as KernelResponse;
            done(() => {
              try {
                child.kill();
              } catch {
                // One-shot worker; nothing to reuse.
              }
              resolve(res);
            });
            return;
          }
        }
        i = buf.indexOf("\n");
      }
    });
    if (!child.stdin) {
      fail(new Error("kernel worker spawn failed"));
      return;
    }
    child.stdin.write(`${JSON.stringify(body)}\n`, (err) => {
      if (err) fail(err instanceof Error ? err : new Error(String(err)));
    });
  });
}

function graphSection(title: string, lines: string[]): string {
  if (lines.length === 0) return `${title}: (none)`;
  return `${title}:\n${lines.map((l) => `- ${l}`).join("\n")}`;
}

// Graph projection for the final writer: Muse renders prose but cites only
// graph nodes/decisions/evidence; unresolved + incomplete_guard are explicit
// so the answer never masquerades a partial graph as complete.
function graphProjection(input: {
  objective: string;
  nodes: KernelGraphNode[];
  decisions: Record<string, unknown>[];
  unresolved: string[];
  incompleteGuard: boolean;
  escalated: boolean;
}): string {
  const nodeById: Record<string, KernelGraphNode> = {};
  for (const n of input.nodes) nodeById[n.node_id] = n;
  const nodeLines = input.nodes.map(
    (n) =>
      `[${n.node_id}] (${n.status}) q=${JSON.stringify(n.question)} deps=${n.depends_on.length ? n.depends_on.join(",") : "none"}`,
  );
  const decisionLines = input.decisions.map((d, i) => {
    const rec = d as Record<string, unknown>;
    const dtype = typeof rec.decision_type === "string" ? rec.decision_type : typeof rec.type === "string" ? rec.type : "decision";
    const dnode = typeof rec.node_id === "string" ? rec.node_id : typeof rec.nodeId === "string" ? rec.nodeId : "session";
    const sel = rec.selected !== undefined ? JSON.stringify(rec.selected) : JSON.stringify(rec);
    return `[D${i}] ${dtype} node=${dnode} selected=${sel.slice(0, 300)}`;
  });
  const unresolvedLines = input.unresolved.map((id) => {
    const n = nodeById[id];
    return n ? `[${id}] (${n.status}) ${n.question.slice(0, 200)}` : `[${id}] (unknown node)`;
  });
  const guard = input.incompleteGuard
    ? "INCOMPLETE: the tool-round guard tripped — coverage is partial, say what is missing, never present this as converged."
    : "Guard: not tripped — still, unresolved items below are open, never present them as answered.";
  return [
    `OBJECTIVE (verbatim user intent; never restate or change it): ${input.objective}`,
    graphSection("NODES", nodeLines),
    graphSection("JEV DECISIONS (persisted dispositions; the only authority)", decisionLines),
    graphSection("UNRESOLVED (explicitly open; cite as gaps, never answer from memory)", unresolvedLines),
    guard,
    "AUTHORITY: project this graph into prose only. Cite only node ids, decision records, and evidence ids above. No invented conclusions, no buy/sell/hold/order/portfolio/committee verdicts — research never decides, the user does.",
  ].join("\n\n");
}

export async function runKernelAgent(
  prompt: string,
  emit: (e: AgentEvent) => void,
  opts?: RunKernelOpts,
): Promise<void> {
  const t0 = performance.now();
  const deps = opts?.deps ?? {};
  const spawnFn = deps.spawnFn ?? defaultSpawn;
  const reasonFn = deps.reason ?? reason;
  const timeoutMs = deps.timeoutMs ?? opts?.deadlineMs ?? WORKER_TIMEOUT_MS;
  const python = deps.python ?? (existsSync(REPO_PYTHON) ? REPO_PYTHON : existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3");
  const workerPath = deps.workerPath ?? WORKER;
  const wantTrace = opts?.includeTrace ?? opts?.onTrace !== undefined;
  const asOf = typeof opts?.asOf === "string" && opts.asOf.trim() ? opts.asOf : null;
  const buildMetrics = (
    decisions: number,
    calls: number,
    ev: Evidence[],
    workerMs: number,
    escalations: number,
    fl: Partial<Record<FailureCategory, number>>,
    muse: Metrics["muse"],
  ): Metrics => ({
    totalMs: performance.now() - t0,
    // ponytail: kernel reports no routing/execution split yet; worker wall time sits on tools, TS routing is 0.
    needle: { calls: decisions, totalMs: 0, escalations },
    tools: { calls, totalMs: workerMs },
    muse,
    evidence: { count: ev.length, characters: ev.reduce((n, e) => n + e.content.length, 0) },
    failures: fl,
  });

  emit({ type: "agent_start", prompt });
  let res: KernelResponse;
  const workerStart = performance.now();
  try {
    res = await callWorker({ id: "1", op: "run", prompt, asOf, deadlineMs: timeoutMs }, { signal: opts?.signal, timeoutMs, python, workerPath, spawnFn });
  } catch (err) {
    // ponytail: needle-down shape — worker failure still ends at done, never throws to the route.
    const msg = err instanceof Error ? err.message : String(err);
    emit({ type: "tool_failed", tool: "needle", category: "provider_error", preview: msg.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: msg.slice(0, 160) });
    emit({ type: "done", metrics: buildMetrics(0, 0, [], 0, 1, { provider_error: 1 }, { calls: 0, totalMs: 0 }) });
    return;
  }
  const workerMs = performance.now() - workerStart;
  // Canonical toolExecutions; legacy needleDecisions alias kept one release for compat.
  const rawExec = Array.isArray(res.toolExecutions) ? res.toolExecutions : res.needleDecisions;
  const decisions = Array.isArray(rawExec) ? rawExec : [];
  const calls = Array.isArray(res.toolCalls) ? res.toolCalls : [];
  const nodes: KernelGraphNode[] = (Array.isArray(res.nodes) ? res.nodes : [])
    .filter((n) => n && typeof n.node_id === "string")
    .map((n) => ({
      node_id: String(n.node_id),
      question: String(n.question ?? ""),
      status: String(n.status ?? ""),
      depends_on: Array.isArray(n.depends_on) ? n.depends_on.map(String) : [],
    }));
  const persisted: Record<string, unknown>[] = Array.isArray(res.decisions) ? res.decisions : [];
  const unresolved: string[] = Array.isArray(res.unresolved) ? res.unresolved.map(String) : [];
  const incompleteGuard = res.incompleteGuard ?? res.incomplete_guard ?? false;
  const objective = typeof res.objective === "string" && res.objective ? res.objective : prompt;
  const evidence: Evidence[] = (Array.isArray(res.evidence) ? res.evidence : []).map((e) => ({
    id: String(e.id),
    source: e.source ?? "kernel",
    ...(e.title !== undefined ? { title: e.title } : {}),
    ...(e.url !== undefined ? { url: e.url } : {}),
    retrievedAt: e.retrievedAt ?? new Date().toISOString(),
    content: String(e.content ?? ""),
  }));
  const byId: Record<string, Evidence> = {};
  for (const e of evidence) byId[e.id] = e;
  const failures: Partial<Record<FailureCategory, number>> = {};
  if (res.failures && typeof res.failures === "object") {
    for (const [k, v] of Object.entries(res.failures)) if (typeof v === "number") failures[k as FailureCategory] = v;
  }
  const countFailure = (c: FailureCategory): void => {
    failures[c] = (failures[c] ?? 0) + 1;
  };
  // ponytail: one trace helper over the live worker fields; no field-specific builders.
  const traceOf = (
    objective: string,
    evidence: Evidence[],
    unresolved: string[],
    incompleteGuard: boolean,
    escalated: boolean,
    failures: Partial<Record<FailureCategory, number>>,
  ): KernelTrace => {
    const fullEvidence =
      Array.isArray(res.evidenceRecords) && res.evidenceRecords.length > 0
        ? res.evidenceRecords.map((r) => ({ ...r }))
        : evidence.map((e) => ({ ...e }));
    const fullNodes =
      Array.isArray(res.nodeRecords) && res.nodeRecords.length > 0
        ? res.nodeRecords.map((n) => ({ ...n }))
        : nodes.map((n) => ({ ...n }));
    const attempts = (Array.isArray(res.attempts) ? res.attempts : []).map((a) => {
      const rec = { ...a };
      const args = rec.arguments;
      // Worker attempt arguments are JSON objects by construction.
      const argsRecord = args as Record<string, unknown>;
      if (typeof args === "object" && args !== null && !Array.isArray(args)) rec.arguments = redactArgs(argsRecord);
      return rec;
    });
    const claims: Record<string, unknown>[] = [];
    for (const d of Array.isArray(res.dossiers) ? res.dossiers : []) {
      for (const key of ["findings", "claims", "relationships"] as const) {
        const arr = d[key];
        if (Array.isArray(arr)) {
          for (const c of arr) {
            if (typeof c === "object" && c !== null && !Array.isArray(c)) claims.push({ ...c });
          }
        }
      }
    }
    const failureCounts: Record<string, number> = {};
    for (const [k, v] of Object.entries(failures)) if (typeof v === "number") failureCounts[k] = v;
    return {
      session: res.session !== undefined ? res.session : null,
      sessionId: typeof res.sessionId === "string" && res.sessionId ? res.sessionId : "",
      asOf: typeof res.asOf === "string" ? res.asOf : asOf,
      objective,
      evidence: fullEvidence,
      nodes: fullNodes,
      nodeRecords: fullNodes,
      decisions: persisted.map((d) => ({ ...d })),
      unresolved: [...unresolved],
      incompleteGuard,
      guardState: { incompleteGuard, escalated },
      attempts,
      toolExecutions: decisions.map((d) => ({ ...d, arguments: redactArgs(d.arguments ?? {}) })),
      toolCalls: calls.map((c) => ({ ...c })),
      failures: failureCounts,
      escalations: res.escalations ?? 0,
      escalated,
      jobs: Array.isArray(res.jobs) ? res.jobs : [],
      events: Array.isArray(res.events) ? res.events : [],
      dossiers: Array.isArray(res.dossiers) ? res.dossiers : [],
      claims,
      coverageArtifacts: Array.isArray(res.coverageArtifacts) ? res.coverageArtifacts : [],
      toolResults: Array.isArray(res.toolResults) ? res.toolResults : [],
      modelPrompt: { objective, nodes: nodes.length, decisions: persisted.length, unresolved: unresolved.length },
    };
  };
  const emitTrace = (
    objective: string,
    evidence: Evidence[],
    unresolved: string[],
    incompleteGuard: boolean,
    escalated: boolean,
    failures: Partial<Record<FailureCategory, number>>,
  ): void => {
    if (!wantTrace) return;
    const trace = traceOf(objective, evidence, unresolved, incompleteGuard, escalated, failures);
    opts?.onTrace?.(trace);
    emit({ type: "evaluation_trace", trace });
  };

  if (typeof res.error === "string" && res.error) {
    countFailure("provider_error");
    emit({ type: "tool_failed", tool: "needle", category: "provider_error", preview: res.error.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: res.error.slice(0, 160) });
    emitTrace(objective, evidence, unresolved, incompleteGuard, true, failures);
    emit({ type: "done", metrics: buildMetrics(decisions.length, calls.length, evidence, workerMs, (res.escalations ?? 0) + 1, failures, { calls: 0, totalMs: 0 }) });
    return;
  }

  // ponytail: index-aligned replay — worker lists decisions and calls in execution order, so calls[i] belongs to decisions[i]; a trailing escalate decision has no call.
  const termCategory = res.terminal && typeof res.terminal === "object" ? res.terminal.category : undefined;
  let toolFailedEmitted = false;
  const emitCall = (c: KernelToolCall): void => {
    emit({ type: "tool_start", tool: c.tool });
    if (c.ok) {
      const ev = c.evidenceId ? byId[c.evidenceId] : undefined;
      emit({ type: "tool_result", tool: c.tool, evidenceId: c.evidenceId, preview: (ev?.content ?? "").slice(0, 160) });
    } else {
      const preview = (c.error ?? "tool failed").slice(0, 160);
      emit({ type: "tool_result", tool: c.tool, preview });
      emit({ type: "tool_failed", tool: c.tool, category: c.category ?? termCategory ?? "tool_error", preview });
      toolFailedEmitted = true;
    }
  };
  for (let i = 0; i < decisions.length; i++) {
    const d = decisions[i];
    emit({ type: "needle_decision", step: d.step ?? i, tool: d.tool ?? null, arguments: redactArgs(d.arguments ?? {}), confidence: d.confidence ?? null });
    if (i < calls.length) emitCall(calls[i]);
  }
  for (let i = decisions.length; i < calls.length; i++) emitCall(calls[i]);

  if (res.terminal && typeof res.terminal === "object") {
    const { category, message } = res.terminal;
    countFailure(category);
    if (!toolFailedEmitted) {
      emit({ type: "tool_failed", tool: calls.length > 0 ? calls[calls.length - 1].tool : "worker", category, preview: message.slice(0, 160) });
    }
    emit({ type: "failed", category, message: message.slice(0, 160) });
    emitTrace(objective, evidence, unresolved, incompleteGuard, true, failures);
    emit({ type: "done", metrics: buildMetrics(decisions.length, calls.length, evidence, workerMs, (res.escalations ?? 0) + 1, failures, { calls: 0, totalMs: 0 }) });
    return;
  }

  emit({ type: "reasoning_start", model: MUSE_MODEL });
  const tm = performance.now();
  let usage: MuseUsage;
  try {
    const projection = graphProjection({ objective, nodes, decisions: persisted, unresolved, incompleteGuard, escalated: res.escalated ?? false });
    const r = await reasonFn({
      prompt: `${prompt}\n\n${projection}`,
      evidence,
      escalated: res.escalated ?? false,
      direct: (res.escalated ?? false) && evidence.length === 0,
      objective,
      nodes,
      decisions: persisted,
      unresolved,
      incompleteGuard,
      onDelta: (text) => emit({ type: "answer_delta", text }),
    });
    usage = r.usage;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    countFailure("provider_error");
    emit({ type: "tool_failed", tool: "muse", category: "provider_error", preview: msg.slice(0, 160) });
    emit({ type: "failed", category: "provider_error", message: msg.slice(0, 160) });
    emitTrace(objective, evidence, unresolved, incompleteGuard, res.escalated ?? false, failures);
    emit({ type: "error", message: msg });
    return;
  }
  emitTrace(objective, evidence, unresolved, incompleteGuard, res.escalated ?? false, failures);
  emit({ type: "done", metrics: buildMetrics(decisions.length, calls.length, evidence, workerMs, res.escalations ?? 0, failures, { calls: 1, totalMs: performance.now() - tm, ...usage }) });
}
